import sqlite3
import os
import logging
import requests
import pytz
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

# Carga de variables de entorno desde el archivo .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Configuración de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cobro_bot")

app = FastAPI(title="CobroBot SaaS")

# ==============================================================================
# CREDENCIALES Y CONFIGURACIÓN VÍA VARIABLES DE ENTORNO
# ==============================================================================
SECRET_KEY = os.getenv("SECRET_KEY", "cobrobot_secret_key_saas_2026_produccion")
INSTANCE_ID = os.getenv("ULTRAMSG_INSTANCE_ID", "instance192749")
TOKEN = os.getenv("ULTRAMSG_TOKEN", "m00oxwpc3qf3wk0p")
BASE_URL = f"https://api.ultramsg.com/{INSTANCE_ID}/messages/chat"
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "51")

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

DB_NAME = "cobro_bot.db"


# --- BASE DE DATOS ---
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                empresa TEXT NOT NULL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clientes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                nombre TEXT NOT NULL,
                telefono TEXT NOT NULL,
                monto REAL NOT NULL,
                forma_pago TEXT NOT NULL,
                fecha_pago TEXT NOT NULL,
                estado TEXT DEFAULT 'Pendiente',
                FOREIGN KEY (usuario_id) REFERENCES usuarios (id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS configuraciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER UNIQUE NOT NULL,
                plantilla TEXT NOT NULL,
                suscripcion_hasta TEXT NOT NULL,
                FOREIGN KEY (usuario_id) REFERENCES usuarios (id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bitacora_whatsapp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                nombre_cliente TEXT NOT NULL,
                telefono TEXT NOT NULL,
                mensaje TEXT NOT NULL,
                fecha_hora TEXT NOT NULL,
                estado TEXT NOT NULL,
                respuesta_api TEXT NOT NULL,
                FOREIGN KEY (usuario_id) REFERENCES usuarios (id)
            )
        ''')
        conn.commit()
    finally:
        conn.close()


init_db()


# --- FUNCIONES AUXILIARES ---
def obtener_usuario_actual(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM usuarios WHERE id = ?", (user_id,))
        return cursor.fetchone()
    finally:
        conn.close()


def formatear_telefono(telefono: str) -> str:
    """ Limpia caracteres no numéricos y antepone el código de país (+51) si falta. """
    num_limpio = "".join(filter(str.isdigit, str(telefono)))
    if not num_limpio:
        return ""
    
    if len(num_limpio) == 9:
        num_limpio = f"{DEFAULT_COUNTRY_CODE}{num_limpio}"
    
    return f"+{num_limpio}"


PLANTILLA_POR_DEFECTO = (
    "Hola {nombre}, te saludamos de {empresa}. Te recordamos que el día {fecha} "
    "vence tu pago de S/ {monto} por {forma_pago}. Por favor realiza tu abono a tiempo. ¡Muchas gracias!"
)

PLANTILLA_AGRADECIMIENTO = (
    "¡Muchas gracias por tu pago, {nombre}! Hemos registrado con éxito tu abono de S/ {monto}. "
    "Atentamente {empresa}."
)


def registrar_bitacora(usuario_id: int, cliente_nombre: str, telefono: str, mensaje: str, estado: str, respuesta: str):
    conn = get_db()
    cursor = conn.cursor()
    fecha_hora = datetime.now(pytz.timezone('America/Lima')).strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor.execute('''
            INSERT INTO bitacora_whatsapp (usuario_id, nombre_cliente, telefono, mensaje, fecha_hora, estado, respuesta_api)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (usuario_id, cliente_nombre, telefono, mensaje, fecha_hora, estado, respuesta))
        conn.commit()
    finally:
        conn.close()


def enviar_mensaje_whatsapp(telefono: str, mensaje: str, usuario_id: int, cliente_nombre: str):
    telefono_formateado = formatear_telefono(telefono)
    
    payload = {
        "token": TOKEN,
        "to": telefono_formateado,
        "body": mensaje
    }
    
    logger.info(f"Enviando WhatsApp a {telefono_formateado}...")
    try:
        response = requests.post(BASE_URL, data=payload, timeout=10)
        res_json = response.json()
        logger.info(f"Respuesta UltraMsg: {res_json}")
        
        if res_json.get("sent") == "true" or "id" in res_json:
            registrar_bitacora(usuario_id, cliente_nombre, telefono_formateado, mensaje, "Exitoso", str(res_json))
            return True, f"Enviado con éxito a {telefono_formateado}"
        else:
            registrar_bitacora(usuario_id, cliente_nombre, telefono_formateado, mensaje, "Fallido", str(res_json))
            return False, f"Error UltraMsg: {res_json}"
    except Exception as e:
        logger.error(f"Excepción al enviar WhatsApp: {e}")
        registrar_bitacora(usuario_id, cliente_nombre, telefono_formateado, mensaje, "Fallido", str(e))
        return False, f"Excepción: {str(e)}"


# --- RUTAS DE DOCUMENTOS LEGALES ---
@app.get("/privacidad", response_class=HTMLResponse)
async def view_privacidad(request: Request):
    return templates.TemplateResponse(request=request, name="privacidad.html", context={})


@app.get("/terminos", response_class=HTMLResponse)
async def view_terminos(request: Request):
    return templates.TemplateResponse(request=request, name="terminos.html", context={})


# --- AUTENTICACIÓN ---
@app.get("/login", response_class=HTMLResponse)
async def view_login(request: Request):
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login", response_class=HTMLResponse)
async def do_login(
    request: Request,
    usuario: Optional[str] = Form(None),
    username: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    clave: Optional[str] = Form(None)
):
    usr = usuario or username
    pwd = password or clave

    if not usr or not pwd:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Por favor completa todos los campos."}
        )

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM usuarios WHERE usuario = ? AND password = ?", (usr, pwd))
        user = cursor.fetchone()
    finally:
        conn.close()

    if user:
        request.session["user_id"] = user["id"]
        request.session["usuario"] = user["usuario"]
        request.session["empresa"] = user["empresa"]
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"error": "Usuario o contraseña incorrectos."}
    )


@app.get("/registro", response_class=HTMLResponse)
async def view_registro(request: Request):
    return templates.TemplateResponse(request=request, name="registro.html", context={"error": None})


@app.post("/registro", response_class=HTMLResponse)
async def do_registro(
    request: Request,
    usuario: Optional[str] = Form(None),
    username: Optional[str] = Form(None),
    user: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    clave: Optional[str] = Form(None),
    pass_val: Optional[str] = Form(None, alias="pass"),
    empresa: Optional[str] = Form(None),
    company: Optional[str] = Form(None),
    nombre_empresa: Optional[str] = Form(None)
):
    usr = usuario or username or user
    pwd = password or clave or pass_val
    emp = empresa or company or nombre_empresa

    if not usr or not pwd or not emp:
        return templates.TemplateResponse(
            request=request,
            name="registro.html",
            context={"error": "Por favor llena todos los campos del formulario."}
        )

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO usuarios (usuario, password, empresa) VALUES (?, ?, ?)",
            (usr, pwd, emp)
        )
        conn.commit()
        usuario_id = cursor.lastrowid

        # Los nuevos usuarios se registran con la suscripción VENCIDA por defecto (requieren pago)
        fecha_vence_vencida = "2026-01-01"
        cursor.execute(
            "INSERT INTO configuraciones (usuario_id, plantilla, suscripcion_hasta) VALUES (?, ?, ?)",
            (usuario_id, PLANTILLA_POR_DEFECTO, fecha_vence_vencida)
        )
        conn.commit()

        request.session["user_id"] = usuario_id
        request.session["usuario"] = usr
        request.session["empresa"] = emp
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    except sqlite3.IntegrityError:
        return templates.TemplateResponse(
            request=request,
            name="registro.html",
            context={"error": f"El usuario '{usr}' ya existe."}
        )
    finally:
        conn.close()


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


# --- PANEL PRINCIPAL ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = obtener_usuario_actual(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM configuraciones WHERE usuario_id = ?", (user["id"],))
        config = cursor.fetchone()

        hoy_str = datetime.now(pytz.timezone('America/Lima')).strftime("%Y-%m-%d")

        if not config:
            fecha_vence_vencida = "2026-01-01"
            cursor.execute(
                "INSERT INTO configuraciones (usuario_id, plantilla, suscripcion_hasta) VALUES (?, ?, ?)",
                (user["id"], PLANTILLA_POR_DEFECTO, fecha_vence_vencida)
            )
            conn.commit()
            plantilla = PLANTILLA_POR_DEFECTO
            suscripcion_hasta = fecha_vence_vencida
        else:
            plantilla = config["plantilla"]
            suscripcion_hasta = config["suscripcion_hasta"]

        suscripcion_activa = suscripcion_hasta >= hoy_str
        # Estado limpio sin mostrar fechas técnicas feas en pantalla
        suscripcion_estado = "Activa" if suscripcion_activa else "Vencido (Requiere Renovación)"

        cursor.execute("SELECT * FROM clientes WHERE usuario_id = ? ORDER BY id DESC", (user["id"],))
        clientes = cursor.fetchall()

        cursor.execute("SELECT SUM(monto) as total FROM clientes WHERE usuario_id = ? AND estado = 'Pagado'", (user["id"],))
        total_cobrado = cursor.fetchone()["total"] or 0.0

        cursor.execute("SELECT SUM(monto) as total FROM clientes WHERE usuario_id = ? AND estado != 'Pagado'", (user["id"],))
        total_pendiente = cursor.fetchone()["total"] or 0.0

        cursor.execute("SELECT * FROM bitacora_whatsapp WHERE usuario_id = ? ORDER BY id DESC LIMIT 50", (user["id"],))
        historial = cursor.fetchall()
    finally:
        conn.close()

    mensaje_alerta = request.session.pop("mensaje_alerta", None)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "usuario": user["usuario"],
            "user_id": user["id"],
            "empresa": user["empresa"],
            "clientes": clientes,
            "plantilla": plantilla,
            "suscripcion_estado": suscripcion_estado,
            # Se oculta la fecha enviando un texto vacío o neutro al template
            "suscripcion_vence": "", 
            "suscripcion_activa": suscripcion_activa,
            "total_cobrado": total_cobrado,
            "total_pendiente": total_pendiente,
            "historial": historial,
            "mensaje_alerta": mensaje_alerta
        }
    )


# --- OPERACIONES DE CLIENTES ---
@app.post("/agregar")
async def agregar_cliente(
    request: Request,
    nombre: str = Form(...),
    telefono: str = Form(...),
    monto: float = Form(...),
    forma_pago: str = Form(...),
    fecha_pago: str = Form(...)
):
    user = obtener_usuario_actual(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            INSERT INTO clientes (usuario_id, nombre, telefono, monto, forma_pago, fecha_pago, estado)
            VALUES (?, ?, ?, ?, ?, ?, 'Pendiente')
        ''', (user["id"], nombre, telefono, monto, forma_pago, fecha_pago))
        conn.commit()
    finally:
        conn.close()

    request.session["mensaje_alerta"] = "Cliente agregado correctamente."
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/editar/{cliente_id}")
async def editar_cliente(
    request: Request,
    cliente_id: int,
    nombre: str = Form(...),
    telefono: str = Form(...),
    monto: float = Form(...),
    forma_pago: str = Form(...),
    fecha_pago: str = Form(...)
):
    user = obtener_usuario_actual(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            UPDATE clientes
            SET nombre = ?, telefono = ?, monto = ?, forma_pago = ?, fecha_pago = ?
            WHERE id = ? AND usuario_id = ?
        ''', (nombre, telefono, monto, forma_pago, fecha_pago, cliente_id, user["id"]))
        conn.commit()
    finally:
        conn.close()

    request.session["mensaje_alerta"] = f"Cliente #{cliente_id} actualizado."
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/marcar_pagado/{cliente_id}")
async def marcar_pagado(request: Request, cliente_id: int):
    user = obtener_usuario_actual(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM clientes WHERE id = ? AND usuario_id = ?", (cliente_id, user["id"]))
        cliente = cursor.fetchone()

        if cliente:
            cursor.execute("UPDATE clientes SET estado = 'Pagado' WHERE id = ? AND usuario_id = ?", (cliente_id, user["id"]))
            conn.commit()

            mensaje_gracias = PLANTILLA_AGRADECIMIENTO.format(
                nombre=cliente["nombre"],
                monto=f"{cliente['monto']:.2f}",
                empresa=user["empresa"]
            )
            enviar_mensaje_whatsapp(cliente["telefono"], mensaje_gracias, user["id"], cliente["nombre"])
            request.session["mensaje_alerta"] = f"Pago registrado. WhatsApp de agradecimiento enviado a {cliente['nombre']}."
    finally:
        conn.close()

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/eliminar/{cliente_id}")
async def eliminar_cliente(request: Request, cliente_id: int):
    user = obtener_usuario_actual(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM clientes WHERE id = ? AND usuario_id = ?", (cliente_id, user["id"]))
        conn.commit()
    finally:
        conn.close()

    request.session["mensaje_alerta"] = "Registro eliminado."
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/guardar_plantilla")
async def guardar_plantilla(request: Request, plantilla_mensaje: str = Form(...)):
    user = obtener_usuario_actual(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE configuraciones SET plantilla = ? WHERE usuario_id = ?", (plantilla_mensaje, user["id"]))
        conn.commit()
    finally:
        conn.close()

    request.session["mensaje_alerta"] = "Plantilla de mensaje guardada correctamente."
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/restablecer_plantilla")
async def restablecer_plantilla(request: Request):
    user = obtener_usuario_actual(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE configuraciones SET plantilla = ? WHERE usuario_id = ?", (PLANTILLA_POR_DEFECTO, user["id"]))
        conn.commit()
    finally:
        conn.close()

    request.session["mensaje_alerta"] = "Plantilla restablecida a la versión por defecto."
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


# --- RENOVACIÓN DE SUSCRIPCIÓN (PROTEGIDA POR ID Y LLAVE 2024M) ---
@app.get("/admin/renovar_suscripcion/{usuario_id_destino}")
async def renovar_suscripcion_admin(request: Request, usuario_id_destino: int, clave: str = ""):
    user = obtener_usuario_actual(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    CLAVE_ADMIN_SECRETA = "2024M"

    if user["id"] != 1 or clave != CLAVE_ADMIN_SECRETA:
        request.session["mensaje_alerta"] = "Acceso denegado: Credenciales de administrador inválidas."
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT suscripcion_hasta FROM configuraciones WHERE usuario_id = ?", (usuario_id_destino,))
        config = cursor.fetchone()

        if config:
            try:
                fecha_actual = datetime.strptime(config["suscripcion_hasta"], "%Y-%m-%d")
            except (ValueError, TypeError):
                fecha_actual = datetime.now(pytz.timezone('America/Lima'))

            hoy = datetime.now(pytz.timezone('America/Lima'))
            base = max(fecha_actual, hoy)
            nueva_fecha = (base + timedelta(days=30)).strftime("%Y-%m-%d")

            cursor.execute("UPDATE configuraciones SET suscripcion_hasta = ? WHERE usuario_id = ?", (nueva_fecha, usuario_id_destino))
            conn.commit()
            request.session["mensaje_alerta"] = f"¡Suscripción del usuario #{usuario_id_destino} renovada exitosamente por 30 días más!"
    finally:
        conn.close()

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/exportar_excel")
async def exportar_excel(request: Request):
    user = obtener_usuario_actual(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, nombre, telefono, monto, forma_pago, fecha_pago, estado FROM clientes WHERE usuario_id = ?", (user["id"],))
        rows = cursor.fetchall()
    finally:
        conn.close()

    csv_data = "ID,Cliente,Telefono,Monto,FormaPago,FechaPago,Estado\n"
    for r in rows:
        csv_data += f"{r['id']},{r['nombre']},{r['telefono']},{r['monto']},{r['forma_pago']},{r['fecha_pago']},{r['estado']}\n"

    return Response(content=csv_data, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=cobranzas.csv"})


# --- RUTA DE PRUEBA MANUAL CON FORZADO ---
@app.get("/probar")
async def probar_envio_manual(request: Request):
    user = obtener_usuario_actual(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    logger.info("--- Ejecutando prueba de envío manual ---")
    ejecutar_notificaciones(forzar_todos_los_pendientes=True)
    request.session["mensaje_alerta"] = "Ejecución manual finalizada. Revisa la consola para más detalles."
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


# --- PROGRAMADOR Y NOTIFICACIONES ---
def ejecutar_notificaciones(forzar_todos_los_pendientes: bool = False):
    conn = get_db()
    cursor = conn.cursor()
    tz_peru = pytz.timezone('America/Lima')
    hoy_str = datetime.now(tz_peru).strftime("%Y-%m-%d")

    logger.info(f"=== Inicio de ejecución de notificaciones (Hoy: {hoy_str}) ===")

    try:
        cursor.execute("SELECT * FROM usuarios")
        usuarios = cursor.fetchall()

        for u in usuarios:
            cursor.execute("SELECT * FROM configuraciones WHERE usuario_id = ?", (u["id"],))
            config = cursor.fetchone()

            if config and config["suscripcion_hasta"] >= hoy_str:
                plantilla = config["plantilla"]
                
                if forzar_todos_los_pendientes:
                    cursor.execute(
                        "SELECT * FROM clientes WHERE usuario_id = ? AND estado = 'Pendiente'",
                        (u["id"],)
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM clientes WHERE usuario_id = ? AND fecha_pago <= ? AND estado = 'Pendiente'",
                        (u["id"], hoy_str)
                    )

                clientes = cursor.fetchall()
                logger.info(f"Usuario '{u['usuario']}': {len(clientes)} cliente(s) pendiente(s) encontrado(s).")

                for c in clientes:
                    mensaje = plantilla.format(
                        nombre=c["nombre"],
                        monto=f"{c['monto']:.2f}",
                        fecha=c["fecha_pago"],
                        forma_pago=c["forma_pago"],
                        empresa=u["empresa"]
                    )
                    exito, detalle = enviar_mensaje_whatsapp(c["telefono"], mensaje, u["id"], c["nombre"])
                    if exito:
                        cursor.execute("UPDATE clientes SET estado = 'Enviado' WHERE id = ?", (c["id"],))
                        conn.commit()
            else:
                logger.warning(f"Usuario '{u['usuario']}' tiene la suscripción inactiva o no configurada.")
    except Exception as e:
        logger.error(f"Error en ejecutar_notificaciones: {e}")
    finally:
        conn.close()
    logger.info("=== Fin de ejecución de notificaciones ===")


# ==============================================================================
# INICIALIZACIÓN DEL PROGRAMADOR EN PRODUCCIÓN (08:00, 12:00, 18:00, 22:00)
# ==============================================================================
tz_lima = pytz.timezone('America/Lima')
scheduler = BackgroundScheduler(timezone=tz_lima)

@app.on_event("startup")
def arrancar_scheduler():
    scheduler.add_job(
        ejecutar_notificaciones,
        'cron',
        hour="8,12,18,22",
        minute=0,
        id="tarea_notificaciones_produccion",
        misfire_grace_time=600
    )
    scheduler.start()
    
    hora_actual = datetime.now(tz_lima).strftime("%Y-%m-%d %H:%M:%S")
    logger.info("==================================================")
    logger.info(f" PROGRAMADOR DE PRODUCCIÓN INICIADO - Hora local: {hora_actual}")
    logger.info(" Horarios de envío automático diario: 08:00, 12:00, 18:00 y 22:00 hs")
    logger.info("==================================================")

@app.on_event("shutdown")
def apagar_scheduler():
    scheduler.shutdown()