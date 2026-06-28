from pydantic import BaseModel, Field
from typing import Optional
import uuid


class User(BaseModel):
    id: int
    name: str = Field(min_length=3, max_length=20)
    email: str = Field(min_length=6, max_length=20, pattern=r"^\S+@\S+\.\S+$")
    password: str = Field(min_length=6, max_length=20)

class UserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None

class LoginDetails(BaseModel):
    email: str
    password: str

class VerifyOTPDetails(BaseModel):
    email: str
    otp: str

class MultiAgentDetails(BaseModel):
    id: int
    uniqueId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = Field(min_length=3, max_length=20)
    prompt: str
    version: Optional[str]
    status: str
    codingAgent: Optional[str]