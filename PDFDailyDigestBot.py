import os
import sys
import logging
import tempfile
import re
import io
from datetime import datetime
from contextlib import contextmanager
from typing import Dict, List, Union, Callable, Optional, Any, Tuple

# Configuración de logging para PythonAnywhere
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/home/Paste95/telegram_bot.log"),  # Actualiza la ruta con tu nombre de usuario
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Importar bibliotecas necesarias
try:
    # Telegram Bot
    from telegram.ext import Filters   # Solo en la versión previa, 13.7
    #from telegram.constants import ParseMode
    from telegram.ext import (
        Updater, CommandHandler, MessageHandler, CallbackQueryHandler,
        CallbackContext
    )
    from telegram import (
        Update, InlineKeyboardButton, InlineKeyboardMarkup, TelegramError, ParseMode
    )

    # Scheduler para tareas programadas
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    # SQLAlchemy para la base de datos
    from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, ForeignKey
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import sessionmaker, relationship, Session

    # Procesamiento de PDF y EPUB
    import PyPDF2
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

except ImportError as e:
    logger.error(f"Error al importar dependencias: {e}")
    logger.info("Asegúrate de instalar todas las dependencias necesarias con pip:")
    logger.info("pip install python-telegram-bot==13.7 sqlalchemy apscheduler pytz PyPDF2 ebooklib beautifulsoup4")
    sys.exit(1)

# Configuración de la base de datos
# --------------------------------

# Crear la clase base para los modelos
Base = declarative_base()

# Modelos de la base de datos
class User(Base):
    """Modelo para los usuarios del bot"""
    __tablename__ = 'user'

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String(64), unique=True, nullable=False)
    username = Column(String(64))
    first_name = Column(String(64))
    last_name = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    documents = relationship('Document', backref='user', lazy=True)

    def __repr__(self):
        return f"<User {self.id}: {self.telegram_id} ({self.username})>"

class Document(Base):
    """Modelo para los documentos PDF o EPUB subidos por los usuarios"""
    __tablename__ = 'document'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)
    filename = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)  # Contenido completo del texto del documento
    current_position = Column(Integer, default=0)  # Posición actual de lectura
    created_at = Column(DateTime, default=datetime.utcnow)
    active = Column(Boolean, default=True)  # Si este es el documento activo para párrafos diarios

    def __repr__(self):
        return f"<Document {self.id}: {self.filename} (User: {self.user_id})>"

    def get_next_paragraph(self, min_length=100, max_length=500):
        """
        Extrae el siguiente párrafo del contenido del documento basado en la posición actual
        Retorna el párrafo y actualiza la posición actual

        Args:
            min_length: Longitud mínima del párrafo (caracteres)
            max_length: Longitud máxima del párrafo (caracteres)

        Returns:
            tuple: (párrafo, es_final)
                - párrafo: el texto del siguiente párrafo
                - es_final: True si se ha llegado al final del documento
        """
        # Verificar si ya se llegó al final del documento
        if self.current_position >= len(self.content):
            return "Fin del documento.", True

        # Buscar el final del párrafo actual
        remaining_content = self.content[self.current_position:]

        # Buscar el siguiente punto seguido de espacio o nueva línea
        paragraph_end = self.current_position
        current_length = 0
        found_end = False

        for i, char in enumerate(remaining_content):
            current_length += 1

            # Condiciones para finalizar párrafo:
            # 1. Encontramos un punto seguido de espacio o nueva línea
            # 2. Alcanzamos la longitud máxima
            if (char in ['.', '!', '?'] and
                (i + 1 < len(remaining_content) and
                 (remaining_content[i+1] == ' ' or remaining_content[i+1] == '\n'))):

                if current_length >= min_length:
                    paragraph_end = self.current_position + i + 1
                    found_end = True
                    break

            # Si llegamos a la longitud máxima y no hemos encontrado un final natural,
            # buscamos el último espacio dentro del rango para cortar ahí
            if current_length >= max_length:
                # Buscar el último espacio en los últimos 100 caracteres
                last_part = remaining_content[max(0, i-100):i+1]
                last_space = last_part.rfind(' ')

                if last_space != -1:
                    paragraph_end = self.current_position + i - (len(last_part) - last_space) + 1
                else:
                    paragraph_end = self.current_position + i + 1

                found_end = True
                break

        # Si no encontramos un final adecuado, tomamos todo el contenido restante
        if not found_end:
            paragraph_end = len(self.content)

        # Extraer el párrafo
        paragraph = self.content[self.current_position:paragraph_end].strip()

        # Actualizar la posición
        self.current_position = paragraph_end

        # Verificar si hemos llegado al final
        is_final = (self.current_position >= len(self.content))

        return paragraph, is_final

    def get_progress_percentage(self):
        """Calcula el progreso de lectura como porcentaje"""
        if not self.content:
            return 0

        return min(100, int((self.current_position / len(self.content)) * 100))

# Configuración de la base de datos para PythonAnywhere
# Actualiza esto con la ruta correcta para tu usuario
DATABASE_PATH = '/home/Paste95/telegram_bot.db'
DATABASE_URL = f'sqlite:///{DATABASE_PATH}'

# Crear el motor de base de datos
engine = create_engine(DATABASE_URL)

# Crear todas las tablas si no existen
Base.metadata.create_all(engine)

# Crear la sesión
SessionMaker = sessionmaker(bind=engine)

@contextmanager
def get_db_session():
    """Contexto para manejar sesiones de base de datos"""
    session = SessionMaker()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Error en la sesión de base de datos: {e}", exc_info=True)
        raise
    finally:
        session.close()

# Funciones de utilidad para el procesamiento de documentos
# --------------------------------------------------------

def extract_text_from_pdf(file_data: bytes) -> str:
    """
    Extrae el texto completo de un archivo PDF

    Args:
        file_data: Contenido del archivo PDF en bytes

    Returns:
        str: Texto extraído del PDF
    """
    try:
        pdf_file = io.BytesIO(file_data)
        pdf_reader = PyPDF2.PdfReader(pdf_file)

        text = ""
        for page_num in range(len(pdf_reader.pages)):
            page = pdf_reader.pages[page_num]
            text += page.extract_text()
            text += "\n"

        # Limpiar el texto
        text = re.sub(r'\s+', ' ', text)  # Reemplazar múltiples espacios por uno solo
        text = text.strip()

        logger.info(f"PDF procesado con éxito: {len(text)} caracteres extraídos")
        return text
    except Exception as e:
        logger.error(f"Error al procesar el PDF: {e}", exc_info=True)
        return ""

def chapter_to_text(chapter) -> str:
    """
    Extrae el texto de un capítulo de EPUB utilizando BeautifulSoup

    Args:
        chapter: Capítulo del EPUB

    Returns:
        str: Texto extraído del capítulo
    """
    soup = BeautifulSoup(chapter.get_body_content(), 'html.parser')
    text = soup.get_text()
    # Limpiar el texto
    text = re.sub(r'\s+', ' ', text)  # Reemplazar múltiples espacios por uno solo
    return text.strip()

def extract_text_from_epub(file_data: bytes) -> str:
    """
    Extrae el texto completo de un archivo EPUB

    Args:
        file_data: Contenido del archivo EPUB en bytes

    Returns:
        str: Texto extraído del EPUB
    """
    try:
        epub_file = io.BytesIO(file_data)
        book = epub.read_epub(epub_file)

        # Extraer texto de todos los documentos en el libro
        all_text = ""

        # Procesamos todos los elementos que puedan contener texto
        for item in book.get_items():
            if item.get_type() == ebooklib.ITEM_DOCUMENT:
                chapter_text = chapter_to_text(item)
                all_text += chapter_text + "\n\n"

        all_text = all_text.strip()
        logger.info(f"EPUB procesado con éxito: {len(all_text)} caracteres extraídos")
        return all_text
    except Exception as e:
        logger.error(f"Error al procesar el EPUB: {e}", exc_info=True)
        return ""

def extract_text_from_document(file_data: bytes, filename: str) -> str:
    """
    Extrae texto de un documento basado en su extensión

    Args:
        file_data: Contenido del archivo en bytes
        filename: Nombre del archivo con extensión

    Returns:
        str: Texto extraído del documento
    """
    # Determinar el tipo de archivo por la extensión
    filename_lower = filename.lower()

    if filename_lower.endswith('.pdf'):
        return extract_text_from_pdf(file_data)
    elif filename_lower.endswith('.epub'):
        return extract_text_from_epub(file_data)
    else:
        logger.warning(f"Formato de archivo no soportado: {filename}")
        return ""

def get_or_create_user(session: Session, telegram_id: str, username: Optional[str] = None,
                     first_name: Optional[str] = None, last_name: Optional[str] = None) -> User:
    """
    Obtiene un usuario existente o crea uno nuevo

    Args:
        session: Sesión de base de datos
        telegram_id: ID de Telegram del usuario
        username: Nombre de usuario de Telegram (opcional)
        first_name: Nombre del usuario (opcional)
        last_name: Apellido del usuario (opcional)

    Returns:
        User: Usuario obtenido o creado
    """
    user = session.query(User).filter_by(telegram_id=telegram_id).first()

    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            created_at=datetime.utcnow()
        )
        session.add(user)
        session.commit()
        logger.info(f"Nuevo usuario creado: {telegram_id} ({username or 'sin username'})")

    return user

def save_document(session: Session, user: User, filename: str, content: str) -> Document:
    """
    Guarda un nuevo documento para un usuario

    Args:
        session: Sesión de base de datos
        user: Usuario propietario del documento
        filename: Nombre del archivo
        content: Contenido de texto extraído del documento

    Returns:
        Document: Documento creado
    """
    # Desactivar todos los documentos actuales del usuario
    for doc in user.documents:
        doc.active = False

    # Crear el nuevo documento como activo
    document = Document(
        user_id=user.id,
        filename=filename,
        content=content,
        current_position=0,
        created_at=datetime.utcnow(),
        active=True
    )

    session.add(document)
    session.commit()
    logger.info(f"Documento guardado para el usuario {user.telegram_id}: {filename}")

    return document

def get_active_document(session: Session, user: User) -> Optional[Document]:
    """
    Obtiene el documento activo de un usuario

    Args:
        session: Sesión de base de datos
        user: Usuario

    Returns:
        Document: Documento activo o None si no tiene ninguno
    """
    return session.query(Document).filter_by(user_id=user.id, active=True).first()

def get_user_documents(session: Session, user: User) -> List[Document]:
    """
    Obtiene todos los documentos de un usuario

    Args:
        session: Sesión de base de datos
        user: Usuario

    Returns:
        List[Document]: Lista de documentos del usuario
    """
    return session.query(Document).filter_by(user_id=user.id).all()

def activate_document(session: Session, document_id: int, user: User) -> Optional[Document]:
    """
    Activa un documento específico y desactiva los demás

    Args:
        session: Sesión de base de datos
        document_id: ID del documento a activar
        user: Usuario propietario

    Returns:
        Document: Documento activado o None si no se encontró
    """
    # Verificar que el documento pertenezca al usuario
    document = session.query(Document).filter_by(id=document_id, user_id=user.id).first()

    if not document:
        return None

    # Desactivar todos los documentos del usuario
    for doc in user.documents:
        doc.active = (doc.id == document_id)

    session.commit()
    logger.info(f"Documento {document_id} activado para el usuario {user.telegram_id}")

    return document

# Configuración del Bot de Telegram
# ---------------------------------

# Obtener el token del bot desde variables de entorno
# Configuración del Bot de Telegram
# ---------------------------------
from dotenv import load_dotenv

load_dotenv() #Carga las variables desde el archivo.env

BOT_TOKEN = os.getenv("TOKEN")

if not BOT_TOKEN:
    logger.error("No se encontró el token del bot de Telegram en las variables de entorno")
    logger.info("En PythonAnywhere, configura la variable de entorno TELEGRAM_TOKEN en la consola:")
    logger.info("Y añádela también al archivo .bashrc para que persista")
    sys.exit(1)

# Mensajes predefinidos
WELCOME_MESSAGE = """
¡Hola! Soy *PDFDailyDigestBot*, tu asistente para la lectura diaria.

Puedo enviarte fragmentos diarios de tus documentos PDF o EPUB para ayudarte a leerlos poco a poco.

Para comenzar, envíame un archivo PDF o EPUB y lo guardaré para ti. Luego recibirás párrafos diarios de ese documento.

Usa /ayuda para ver la lista completa de comandos disponibles.
"""

HELP_MESSAGE = """
*Comandos disponibles:*

• /ayuda - Muestra este mensaje de ayuda
• /estado - Muestra tu progreso de lectura actual
• /siguiente - Recibe inmediatamente el siguiente párrafo
• /cambiar - Cambia entre tus documentos subidos

Para comenzar, simplemente envíame un archivo PDF o EPUB y automáticamente empezaré a enviarte fragmentos diarios de ese documento.

Los párrafos diarios se envían a las 11:00 AM (UTC+2).
"""

# Funciones del bot
def start(update: Update, context: CallbackContext) -> None:
    """Envía un mensaje cuando se emite el comando /start."""
    user = update.effective_user
    logger.info(f"Comando /start de {user.id} ({user.username or 'sin username'})")

    with get_db_session() as session:
        # Registrar o actualizar usuario
        get_or_create_user(
            session=session,
            telegram_id=str(user.id),
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )

    update.message.reply_text(
        WELCOME_MESSAGE,
        parse_mode=ParseMode.MARKDOWN
    )

def help_command(update: Update, context: CallbackContext) -> None:
    """Envía un mensaje cuando se emite el comando /help o /ayuda."""
    user = update.effective_user
    logger.info(f"Comando /ayuda de {user.id} ({user.username or 'sin username'})")

    update.message.reply_text(
        HELP_MESSAGE,
        parse_mode=ParseMode.MARKDOWN
    )

def status_command(update: Update, context: CallbackContext) -> None:
    """Muestra el estado de lectura del usuario y su progreso."""
    user = update.effective_user
    logger.info(f"Comando /estado de {user.id} ({user.username or 'sin username'})")

    with get_db_session() as session:
        db_user = get_or_create_user(session, str(user.id))
        active_doc = get_active_document(session, db_user)

        if not active_doc:
            update.message.reply_text(
                "No tienes ningún documento activo. Envíame un archivo PDF o EPUB para comenzar.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        progress = active_doc.get_progress_percentage()

        status_message = f"""
*Estado de lectura actual:*

📄 Documento: `{active_doc.filename}`
📊 Progreso: {progress}%
📅 Añadido el: {active_doc.created_at.strftime('%d/%m/%Y')}

Recibirás un párrafo diario a las 11:00 AM (UTC+2).
Usa /siguiente para recibir el próximo párrafo inmediatamente.
"""

        update.message.reply_text(
            status_message,
            parse_mode=ParseMode.MARKDOWN
        )

def next_paragraph_command(update: Update, context: CallbackContext) -> None:
    """Envía el siguiente párrafo inmediatamente."""
    user = update.effective_user
    logger.info(f"Comando /siguiente de {user.id} ({user.username or 'sin username'})")

    with get_db_session() as session:
        db_user = get_or_create_user(session, str(user.id))
        active_doc = get_active_document(session, db_user)

        if not active_doc:
            update.message.reply_text(
                "No tienes ningún documento activo. Envíame un archivo PDF o EPUB para comenzar.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        paragraph, is_final = active_doc.get_next_paragraph()
        session.commit()

        progress = active_doc.get_progress_percentage()

        if is_final:
            message = f"""
*¡Has terminado de leer este documento!* 🎉

"{paragraph}"

📊 Progreso: 100%
📄 Documento: `{active_doc.filename}`

Puedes enviarme otro documento PDF o EPUB para comenzar una nueva lectura o usar /cambiar para seleccionar otro documento que hayas subido anteriormente.
"""
        else:
            message = f"""
*Tu fragmento de lectura:*

"{paragraph}"

📊 Progreso: {progress}%
📄 Documento: `{active_doc.filename}`
"""

        update.message.reply_text(
            message,
            parse_mode=ParseMode.MARKDOWN
        )

def switch_document_command(update: Update, context: CallbackContext) -> None:
    """Permite al usuario cambiar entre documentos subidos."""
    user = update.effective_user
    logger.info(f"Comando /cambiar de {user.id} ({user.username or 'sin username'})")

    with get_db_session() as session:
        db_user = get_or_create_user(session, str(user.id))
        documents = get_user_documents(session, db_user)

        if not documents:
            update.message.reply_text(
                "No tienes ningún documento subido. Envíame un archivo PDF o EPUB para comenzar.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        keyboard = []
        for doc in documents:
            # Marcar el documento activo con un asterisco
            status = "✓ " if doc.active else ""
            progress = doc.get_progress_percentage()
            button_text = f"{status}{doc.filename} ({progress}%)"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"switch_{doc.id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        update.message.reply_text(
            "Selecciona el documento que deseas leer:",
            reply_markup=reply_markup
        )

def handle_switch_callback(update: Update, context: CallbackContext) -> None:
    """Maneja el callback de cambio de documento."""
    query = update.callback_query
    query.answer()

    # Extraer el ID del documento
    document_id = int(query.data.split("_")[1])
    user = update.effective_user

    with get_db_session() as session:
        db_user = get_or_create_user(session, str(user.id))
        document = activate_document(session, document_id, db_user)

        if document:
            progress = document.get_progress_percentage()
            query.edit_message_text(
                f"Documento cambiado a: *{document.filename}*\nProgreso actual: {progress}%",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            query.edit_message_text("No se pudo cambiar el documento. Intenta de nuevo.")

def handle_document_upload(update: Update, context: CallbackContext) -> None:
    """Maneja la subida de archivos (PDF y EPUB)."""
    user = update.effective_user
    document = update.message.document
    filename_lower = document.file_name.lower()

    # Verificar si el archivo es PDF o EPUB
    is_pdf = filename_lower.endswith('.pdf')
    is_epub = filename_lower.endswith('.epub')

    # Registrar recepción del archivo
    file_type = "PDF" if is_pdf else "EPUB" if is_epub else "desconocido"
    logger.info(f"Archivo {file_type} recibido de {user.id} ({user.username or 'sin username'}): {document.file_name}")

    # Verificar tipo de archivo permitido
    if not (is_pdf or is_epub):
        update.message.reply_text(
            "Por favor, envía solo archivos PDF o EPUB.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        # Informar al usuario que estamos procesando el archivo
        processing_message = update.message.reply_text(
            f"Procesando tu archivo {file_type}. Por favor, espera un momento...",
            parse_mode=ParseMode.MARKDOWN
        )

        # Descargar el archivo
        file = context.bot.get_file(document.file_id)

        with tempfile.NamedTemporaryFile(delete=True) as temp_file:
            file.download(custom_path=temp_file.name)

            # Leer el archivo como bytes
            with open(temp_file.name, 'rb') as f:
                file_data = f.read()

        # Extraer texto del documento (PDF o EPUB)
        text = extract_text_from_document(file_data, document.file_name)

        if not text or len(text.strip()) < 10:
            update.message.reply_text(
                f"No se pudo extraer texto del archivo {file_type}. Asegúrate de que contiene texto seleccionable y no sólo imágenes.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # Guardar en la base de datos
        with get_db_session() as session:
            db_user = get_or_create_user(session, str(user.id))
            save_document(session, db_user, document.file_name, text)

        update.message.reply_text(
            f"""
¡Documento {file_type} recibido y procesado con éxito! 📚

📄 Nombre: `{document.file_name}`
📊 Caracteres extraídos: {len(text)}

Recibirás un párrafo diario a las 11:00 AM (UTC+2).
Usa /siguiente para recibir el primer párrafo inmediatamente.
Usa /estado para ver tu progreso actual.
""",
            parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.error(f"Error al procesar {file_type}: {e}", exc_info=True)
        update.message.reply_text(
            f"Ocurrió un error al procesar el archivo {file_type}. Por favor, intenta con otro archivo.",
            parse_mode=ParseMode.MARKDOWN
        )

def handle_message(update: Update, context: CallbackContext) -> None:
    """Maneja los mensajes de texto recibidos."""
    user = update.effective_user
    message = update.message.text
    logger.info(f"Mensaje recibido de {user.id} ({user.username or 'sin username'}): {message[:20]}...")

    # Responder con instrucciones
    update.message.reply_text(
        "Para comenzar, envíame un archivo PDF o EPUB. Usa /ayuda para ver la lista de comandos disponibles.",
        parse_mode=ParseMode.MARKDOWN
    )

def send_daily_paragraph(context: CallbackContext) -> None:
    """
    Envía el párrafo diario a todos los usuarios.
    Esta función es llamada por el programador diariamente.
    """
    logger.info("Iniciando envío de párrafos diarios a todos los usuarios...")

    with get_db_session() as session:
        # Obtener todos los usuarios
        users = session.query(User).all()
        logger.info(f"Total de usuarios: {len(users)}")

        for user in users:
            try:
                # Obtener el documento activo
                active_doc = session.query(Document).filter_by(user_id=user.id, active=True).first()

                if not active_doc:
                    logger.info(f"Usuario {user.telegram_id} no tiene documento activo")
                    continue

                # Obtener el siguiente párrafo
                paragraph, is_final = active_doc.get_next_paragraph()
                session.commit()

                # Obtener el progreso
                progress = active_doc.get_progress_percentage()

                # Construir el mensaje
                if is_final:
                    message = f"""
*¡Has terminado de leer este documento!* 🎉

"{paragraph}"

📊 Progreso: 100%
📄 Documento: `{active_doc.filename}`

Puedes enviarme otro documento PDF o EPUB para comenzar una nueva lectura o usar /cambiar para seleccionar otro documento que hayas subido anteriormente.
"""
                else:
                    message = f"""
*Tu párrafo diario:*

"{paragraph}"

📊 Progreso: {progress}%
📄 Documento: `{active_doc.filename}`
"""

                # Enviar el mensaje
                context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN
                )
                logger.info(f"Párrafo diario enviado a {user.telegram_id}")

            except Exception as e:
                logger.error(f"Error al enviar párrafo diario a {user.telegram_id}: {e}", exc_info=True)

def main() -> None:
    """Función principal para ejecutar el bot de Telegram."""
    logger.info("Iniciando el bot de Telegram en PythonAnywhere...")

    # Inicializar el updater
    updater = Updater(BOT_TOKEN)

    # Obtener el dispatcher para registrar handlers
    dispatcher = updater.dispatcher

    # Registrar handlers de comandos
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("ayuda", help_command))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("estado", status_command))
    dispatcher.add_handler(CommandHandler("siguiente", next_paragraph_command))
    dispatcher.add_handler(CommandHandler("cambiar", switch_document_command))

    # Registrar handler para los callbacks (botones)
    dispatcher.add_handler(CallbackQueryHandler(handle_switch_callback, pattern="^switch_"))

    # Registrar handler para documentos (PDF y EPUB)
    dispatcher.add_handler(MessageHandler(Filters.document, handle_document_upload))

    # Registrar handler para mensajes de texto
    dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    # Configurar el programador para enviar párrafos diarios
    scheduler = BackgroundScheduler(timezone=pytz.UTC)
    scheduler.add_job(
        lambda: send_daily_paragraph(updater.dispatcher),
        trigger=CronTrigger(hour=9, minute=0, timezone=pytz.UTC),
        id='daily_paragraph_job',
        replace_existing=True
    )
    scheduler.start()
    logger.info("Programador configurado para enviar párrafos diarios a las 11:00 AM (UTC+2)")

    # Iniciar el bot en modo polling
    logger.info("Bot iniciado en modo polling. Presiona Ctrl+C para detener.")
    updater.start_polling()

    # Ejecutar el bot hasta que se presione Ctrl+C
    updater.idle()

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logger.error(f"Error al iniciar el bot: {e}", exc_info=True)
