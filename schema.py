from pydantic import BaseModel, Field
from typing import Optional


# ── User Models ───────────────────────────────────────────

class UserRegister(BaseModel):
    name: str = Field(min_length=3, max_length=20)
    email: str = Field(min_length=6, max_length=50, pattern=r"^\S+@\S+\.\S+$")
    password: str = Field(min_length=6, max_length=20)

class UserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None

class LoginDetails(BaseModel):
    email: str
    password: str


# ── Multi-Agent Models ────────────────────────────────────

class MultiAgentCreate(BaseModel):
    """
    What the user sends to create an agent.
    Everything else (files, tasks, sandbox, memory, coordination)
    is handled entirely by the backend.
    """
    name: str = Field(min_length=3, max_length=50)
    prompt: str
    version: Optional[str] = "1.0.0"
    status: Optional[str] = "active"
    codingAgent: Optional[str] = "CODEX"

class MultiAgentDetails(BaseModel):
    """What the backend returns after creating/fetching an agent."""
    id: int
    uniqueId: str
    name: str
    prompt: str
    filePath: str
    version: Optional[str]
    status: str
    codingAgent: Optional[str]