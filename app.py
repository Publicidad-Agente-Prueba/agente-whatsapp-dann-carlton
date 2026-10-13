"""Agente de WhatsApp de Dann Carlton Barranquilla.

Recibe los mensajes de Callbell por webhook, le pide la respuesta a Claude
y la envía por la API de Callbell. Cuando el tema necesita una persona,
le asigna la conversación al asesor y deja de responder en ese chat.
"""

import hashlib
import hmac
import json
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import requests
from flask import Flask, jsonify, request

# --- Configuración (variables de entorno en Render) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CALLBELL_API_KEY = os.environ.get("CALLBELL_API_KEY", "")
CALLBELL_CHANNEL_UUID = os.environ.get("CALLBELL_CHANNEL_UUID", "")
CALLBELL_WEBHOOK_SECRET = os.environ.get("CALLBELL_WEBHOOK_SECRET", "")
HUMANO_EMAIL = os.environ.get("HUMANO_EMAIL", "")  # correo del asesor en Callbell
MODELO = os.environ.get("MODELO", "claude-opus-5-5")
HORA_INICIO = int(os.environ.get("HORA_INICIO", "8"))
HORA_FIN = int(os.environ.get("HORA_FIN", "18"))
# Minutos que el bot se calla cuando un asesor escribe a mano; luego manda un mensaje de seguimiento
PAUSA_ASESOR_MIN = float(os.environ.get("PAUSA_ASESOR_MIN", "3"))
# Minutos que el bot se calla después de pasarle el chat a un asesor
PAUSA_TRASPASO_MIN = float(os.environ.get("PAUSA_TRASPASO_MIN", "10"))
# Lista opcional de celulares permitidos, separados por coma (con código de país). Vacío = cualquiera.
NUMEROS_PERMITIDOS = {
    "".join(c for c in n if c.isdigit())
    for n in os.environ.get("NUMEROS_PERMITIDOS", "").split(",") if n.strip()
}
MENSAJE_SEGUIMIENTO = os.environ.get(
    "MENSAJE_SEGUIMIENTO",
    "Hola, ¿sigues por aquí? Si te interesa continuar con la conversación, con gusto te seguimos atendiendo.",
)
ETIQUETA_SILENCIO = os.environ.get("ETIQUETA_SILENCIO", "humano").lower()

CALLBELL_URL = "https://api.callbell.eu/v1"
ZONA = ZoneInfo("America/Bogota")
MAX_HISTORIAL = 20  # mensajes que recuerda por conversación

INSTRUCCIONES = (Path(__file__).parent / "conocimiento.md").read_text(encoding="utf-8")

FORMATO_SALIDA = {
    "format": {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "respuesta": {"type": "string"},
                "pasar_a_humano": {"type": "boolean"},
                "motivo": {"type": "string"},
            },
            "required": ["respuesta", "pasar_a_humano", "motivo"],
            "additionalProperties": False,
        },
    },
    "effort": "low",
}

MENSAJE_ERROR = (
    "Gracias por escribirnos. En este momento no puedo responder. "
    "Un asesor de Dann Carlton Barranquilla te contactará pronto."
)

app = Flask(__name__)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# --- Estado en memoria (se reinicia si el servidor se reinicia) ---
historiales = defaultdict(lambda: deque(maxlen=MAX_HISTORIAL))
pausados = {}  # contacto -> hora (epoch) hasta la que el bot no responde
modo_pausa = {}  # contacto -> "asesor" (escribió a mano) o "traspaso" (el bot pasó el chat)
ultimo_cliente = {}  # contacto -> hora (epoch) del último mensaje del cliente
seguimientos = {}  # contacto -> marca del seguimiento programado
enviados_por_bot = defaultdict(lambda: deque(maxlen=20))  # textos que mandó el bot
procesados = deque(maxlen=1000)  # evita responder dos veces el mismo mensaje
candados = defaultdict(threading.Lock)
candado_global = threading.Lock()


def log(evento, **datos):
    print(json.dumps({"evento": evento, **datos}, ensure_ascii=False), flush=True)


def firma_valida(cuerpo: bytes, cabecera: str) -> bool:
    if not CALLBELL_WEBHOOK_SECRET:
        return True  # sin secreto configurado no se verifica
    try:
        partes = dict(p.split("=", 1) for p in cabecera.split(","))
        t, v1 = partes["t"], partes["v1"]
    except (KeyError, ValueError):
        return False
    if abs(time.time() - int(t)) > 300:
        return False
    esperado = hmac.new(
        CALLBELL_WEBHOOK_SECRET.encode(), t.encode() + b"." + cuerpo, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(esperado, v1)


def enviar_whatsapp(telefono: str, texto: str, asignar: bool = False) -> None:
    cuerpo = {
        "to": telefono,
        "from": "whatsapp",
        "type": "text",
        "channel_uuid": CALLBELL_CHANNEL_UUID,
        "content": {"text": texto},
    }
    if asignar and HUMANO_EMAIL:
        cuerpo["assigned_user"] = HUMANO_EMAIL
    r = requests.post(
        f"{CALLBELL_URL}/messages/send",
        headers={"Authorization": f"Bearer {CALLBELL_API_KEY}"},
        json=cuerpo,
        timeout=20,
    )
    if r.status_code == 400 and "assigned_user" in r.text:
        # El correo de HUMANO_EMAIL no existe en Callbell: enviar igual, sin asignar, para no dejar al cliente sin respuesta
        log("error_asignacion", detalle="HUMANO_EMAIL no existe en la cuenta de Callbell")
        cuerpo.pop("assigned_user", None)
        r = requests.post(
            f"{CALLBELL_URL}/messages/send",
            headers={"Authorization": f"Bearer {CALLBELL_API_KEY}"},
            json=cuerpo,
            timeout=20,
        )
    log("callbell_respuesta", estado=r.status_code, detalle=r.text[:200])
    if r.status_code >= 300:
        log("error_callbell", estado=r.status_code, detalle=r.text[:300])
    r.raise_for_status()


def nota_de_contexto(primer_mensaje: bool) -> str:
    ahora = datetime.now(ZONA)
    habil = ahora.weekday() < 5 and HORA_INICIO <= ahora.hour < HORA_FIN
    dia = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"][ahora.weekday()]
    nota = (
        f"Fecha y hora en Barranquilla: {dia} {ahora:%d/%m/%Y %H:%M}. "
        f"Horario de atención de asesores: lunes a viernes de {HORA_INICIO}:00 a {HORA_FIN}:00. "
        + ("Estás dentro del horario de atención." if habil
           else "Estás fuera del horario de atención.")
    )
    if primer_mensaje:
        nota += (
            " Es el primer mensaje de este cliente: salúdalo en nombre de Dann Carlton "
            "Barranquilla, preséntate como el asistente virtual del hotel y agrega en una "
            "frase que sus datos se tratan según la política de tratamiento de datos del "
            "hotel (Ley 1581 de 2012)."
        )
    return nota


def pedir_respuesta(contacto: str, primer_mensaje: bool) -> dict:
    mensajes = list(historiales[contacto])
    while mensajes and mensajes[0]["role"] != "user":  # la conversación debe abrir con el cliente
        mensajes.pop(0)
    nota = nota_de_contexto(primer_mensaje)
    sistema = [{"type": "text", "text": INSTRUCCIONES, "cache_control": {"type": "ephemeral"}}]
    try:
        respuesta = claude.beta.messages.create(
            model=MODELO,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=sistema,
            output_config=FORMATO_SALIDA,
            messages=mensajes + [{"role": "system", "content": nota}],
        )
    except anthropic.BadRequestError as e:
        # Plan B: petición básica, con la nota de contexto dentro del último mensaje del cliente
        log("plan_b", detalle=str(e)[:300])
        ultimo = mensajes[-1]
        mensajes = mensajes[:-1] + [{"role": "user", "content": f"[Nota interna: {nota}]\n\n{ultimo['content']}"}]
        respuesta = claude.messages.create(
            model=MODELO,
            max_tokens=4000,
            system=sistema,
            output_config=FORMATO_SALIDA,
            messages=mensajes,
        )
    if respuesta.stop_reason == "refusal":
        log("rechazo", contacto=contacto)
        return {"respuesta": MENSAJE_ERROR, "pasar_a_humano": True, "motivo": "El asistente no pudo responder"}
    texto = next((b.text for b in respuesta.content if b.type == "text"), "")
    datos = json.loads(texto)
    u = respuesta.usage
    log("uso_claude", contacto=contacto, entrada=u.input_tokens, salida=u.output_tokens,
        cache_leido=u.cache_read_input_tokens)
    return datos


def atender(payload: dict) -> None:
    contacto_info = payload.get("contact") or {}
    contacto = contacto_info.get("uuid") or payload.get("from", "")
    telefono = payload.get("from", "")
    texto = (payload.get("text") or "").strip()

    if NUMEROS_PERMITIDOS:
        digitos = "".join(c for c in telefono if c.isdigit())
        if digitos not in NUMEROS_PERMITIDOS:
            log("numero_no_permitido", telefono_fin=digitos[-4:])
            return

    ultimo_cliente[contacto] = time.time()

    with candados[contacto]:
        if pausados.get(contacto, 0) > time.time():
            log("bot_en_pausa", contacto=contacto)
            return
        etiquetas = [str(t).lower() for t in contacto_info.get("tags") or []]
        if ETIQUETA_SILENCIO in etiquetas:
            log("silenciado_por_etiqueta", contacto=contacto)
            return

        if not texto:
            texto = "[El cliente envió un archivo, audio o imagen sin texto]"

        primer_mensaje = len(historiales[contacto]) == 0
        historiales[contacto].append({"role": "user", "content": texto})

        try:
            datos = pedir_respuesta(contacto, primer_mensaje)
        except Exception as e:  # cualquier falla: avisar al cliente y pasar a humano
            log("error_claude", contacto=contacto, tipo=type(e).__name__, detalle=str(e)[:300])
            datos = {"respuesta": MENSAJE_ERROR, "pasar_a_humano": True, "motivo": "Error técnico del asistente"}

        respuesta = datos["respuesta"].strip()
        pasar = bool(datos.get("pasar_a_humano"))
        historiales[contacto].append({"role": "assistant", "content": respuesta})
        enviados_por_bot[contacto].append(respuesta)

        try:
            enviar_whatsapp(telefono, respuesta, asignar=pasar)
        except Exception as e:
            log("error_envio", contacto=contacto, tipo=type(e).__name__)
            return

        if pasar:
            pausados[contacto] = time.time() + PAUSA_TRASPASO_MIN * 60
            modo_pausa[contacto] = "traspaso"
            log("pasado_a_humano", contacto=contacto, motivo=datos.get("motivo", ""),
                conversacion=contacto_info.get("conversationHref", ""))
        else:
            log("respondido", contacto=contacto)


def programar_seguimiento(contacto: str, telefono: str) -> None:
    """Pasado el tiempo de pausa, si el cliente no ha vuelto a escribir, el bot le pregunta si sigue interesado."""
    marca = time.time()
    seguimientos[contacto] = marca

    def revisar() -> None:
        with candados[contacto]:
            if seguimientos.get(contacto) != marca:
                return  # el asesor escribió de nuevo y reinició el conteo
            if modo_pausa.get(contacto) != "asesor":
                return
            if ultimo_cliente.get(contacto, 0) > marca:
                return  # el cliente ya escribió; el bot retoma con su próximo mensaje
            enviados_por_bot[contacto].append(MENSAJE_SEGUIMIENTO)
            historiales[contacto].append({"role": "assistant", "content": MENSAJE_SEGUIMIENTO})
            try:
                enviar_whatsapp(telefono, MENSAJE_SEGUIMIENTO)
                log("seguimiento_enviado", contacto=contacto)
            except Exception as e:
                log("error_seguimiento", contacto=contacto, tipo=type(e).__name__)

    temporizador = threading.Timer(PAUSA_ASESOR_MIN * 60, revisar)
    temporizador.daemon = True
    temporizador.start()


def registrar_mensaje_saliente(payload: dict) -> None:
    """Si un asesor escribe a mano en Callbell, el bot se calla un rato en ese chat."""
    contacto = (payload.get("contact") or {}).get("uuid") or payload.get("to", "")
    telefono = payload.get("to", "")
    texto = (payload.get("text") or "").strip()
    if not texto or texto in enviados_por_bot[contacto]:
        return
    ahora = time.time()
    if modo_pausa.get(contacto) == "traspaso" and pausados.get(contacto, 0) > ahora:
        # el chat ya estaba con un asesor: el bot vuelve PAUSA_TRASPASO_MIN después de su último mensaje
        pausados[contacto] = ahora + PAUSA_TRASPASO_MIN * 60
        log("asesor_escribio_tras_traspaso", contacto=contacto)
        return
    pausados[contacto] = ahora + PAUSA_ASESOR_MIN * 60
    modo_pausa[contacto] = "asesor"
    log("asesor_tomo_el_chat", contacto=contacto)
    programar_seguimiento(contacto, telefono)


@app.get("/")
def salud():
    return jsonify({"status": "ok", "servicio": "agente-whatsapp-dann-carlton"})


@app.post("/webhook")
def webhook():
    cuerpo = request.get_data()
    if not firma_valida(cuerpo, request.headers.get("X-Callbell-Signature", "")):
        log("firma_invalida")
        return jsonify({"status": "firma invalida"}), 401

    try:
        evento = json.loads(cuerpo or b"{}")
    except json.JSONDecodeError:
        return jsonify({"status": "ok"})
    if evento.get("event") != "message_created":
        return jsonify({"status": "ok"})

    payload = evento.get("payload") or {}
    if payload.get("channel") not in (None, "whatsapp"):
        return jsonify({"status": "ok"})

    clave = f"{payload.get('from')}|{payload.get('to')}|{payload.get('createdAt')}|{payload.get('text')}"
    with candado_global:
        if clave in procesados:
            return jsonify({"status": "ok"})
        procesados.append(clave)

    if payload.get("status") == "received":
        threading.Thread(target=atender, args=(payload,), daemon=True).start()
    elif payload.get("status") == "sent":
        registrar_mensaje_saliente(payload)

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
