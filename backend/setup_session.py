"""
setup_session.py — Ejecuta este script UNA VEZ para autenticar tu cuenta de Telegram.
Esto creará el archivo .session que el servidor usará para conectarse sin pedir código cada vez.

Uso: python setup_session.py
"""
import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

load_dotenv()

def parse_int_env(name: str, default: int = 0) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default

API_ID       = parse_int_env("TELEGRAM_API_ID")
API_HASH     = os.getenv("TELEGRAM_API_HASH", "")
PHONE        = os.getenv("TELEGRAM_PHONE", "")
SESSION_NAME = os.getenv("TELEGRAM_SESSION", "deezyanax_session")

async def main():
    print("=" * 50)
    print("  DeezYanax — Configuración de sesión Telegram")
    print("=" * 50)
    
    if not API_ID or not API_HASH or not PHONE:
        print("\n❌ ERROR: Faltan variables de entorno.")
        print("   Copia .env.example a .env y completa los datos.")
        print("   Obtén API_ID y API_HASH en: https://my.telegram.org/apps\n")
        return
    
    use_string_session = os.getenv("PRINT_SESSION_STRING", "1").strip() != "0"
    session = StringSession() if use_string_session else SESSION_NAME
    client = TelegramClient(session, API_ID, API_HASH)
    
    print(f"\n📱 Iniciando sesión para el número: {PHONE}")
    print("   Se enviará un código de verificación a tu Telegram...\n")
    
    await client.connect()
    
    if not await client.is_user_authorized():
        await client.send_code_request(PHONE)
        code = input("📩 Ingresa el código que recibiste en Telegram: ").strip()
        
        try:
            await client.sign_in(PHONE, code)
        except SessionPasswordNeededError:
            password = input("🔐 Tu cuenta tiene 2FA. Ingresa tu contraseña: ").strip()
            await client.sign_in(password=password)
    
    me = await client.get_me()
    print(f"\n✅ ¡Sesión creada exitosamente!")
    print(f"   Usuario: {me.first_name} {me.last_name or ''} (@{me.username})")
    if use_string_session:
        print("\n🔐 TELEGRAM_SESSION_STRING para Render:")
        print(client.session.save())
        print("\n   Copia ese valor en Render como variable secreta TELEGRAM_SESSION_STRING.")
    else:
        print(f"   Archivo de sesión: {SESSION_NAME}.session")
    
    # Verificar que podemos hablar con el bot
    print(f"\n🤖 Verificando conexión con @deezload2bot...")
    bot = await client.get_entity("deezload2bot")
    print(f"   Bot encontrado: {bot.username}")
    
    await client.disconnect()
    print("\n🚀 ¡Todo listo! Ahora puedes iniciar el servidor con:")
    print("   uvicorn main:app --reload --port 8000\n")

if __name__ == "__main__":
    asyncio.run(main())
