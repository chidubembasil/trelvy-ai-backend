# import random
# import string
# from fastapi_mail import FastMail, MessageSchema, ConnectionConfig
# import os

# def get_mail_config():
#     return ConnectionConfig(
#         MAIL_USERNAME=os.getenv("MAIL_USERNAME"),
#         MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),
#         MAIL_FROM=os.getenv("MAIL_FROM"),
#         MAIL_PORT=int(os.getenv("MAIL_PORT", 587)),
#         MAIL_SERVER=os.getenv("MAIL_SERVER"),
#         MAIL_STARTTLS=True,
#         MAIL_SSL_TLS=False,
#         USE_CREDENTIALS=True,
#     )

# def generate_otp(length: int = 6) -> str:
#     return "".join(random.choices(string.digits, k=length))

# async def send_otp_email(email: str, otp: str, name: str):
#     conf = get_mail_config()  # called here, not at import time
#     fm = FastMail(conf)
#     message = MessageSchema(
#         subject="Welcome to Trelvy — Your OTP Code",
#         recipients=[email],
#         body=f"""
#         <html>
#         <body style="font-family: Arial, sans-serif; padding: 20px;">
#             <h2>Welcome to Trelvy, {name}! 👋</h2>
#             <p>Your One-Time Password (OTP) is:</p>
#             <h1 style="
#                 background: #f4f4f4;
#                 padding: 20px;
#                 text-align: center;
#                 letter-spacing: 10px;
#                 font-size: 40px;
#                 border-radius: 8px;
#             ">{otp}</h1>
#             <p>This OTP expires in <strong>10 minutes.</strong></p>
#             <p>If you didn't request this, ignore this email.</p>
#             <br/>
#             <p>— The Trelvy Team</p>
#         </body>
#         </html>
#         """,
#         subtype="html"
#     )
#     fm = FastMail(conf)
#     await fm.send_message(message)