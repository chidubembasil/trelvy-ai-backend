from fastapi import FastAPI
from database import db
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from router import agent, userAuth



@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    try:
        yield
    finally: 
        await db.disconnect()

app = FastAPI(lifespan=lifespan)

#CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "https://yourapp.com",
    ],
    allow_credentials=True,           # needed for Authorization header
    allow_methods=["*"],              # GET, POST, PATCH, DELETE etc
    allow_headers=["*"],              # Authorization, Content-Type etc
)

app.include_router(agent.router)
app.include_router(userAuth.router)