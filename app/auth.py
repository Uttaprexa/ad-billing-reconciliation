"""Header-based identity: X-User and X-Role. A stand-in for JWT/OIDC so permissions are easy to demo and test.
In production you would verify a signed token and read these claims from it."""
from enum import Enum

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel


class Role(str, Enum):
    PREPARER = "preparer"   # creates invoices/credits, submits for approval
    APPROVER = "approver"   # approves/rejects, applies payments, reads audit log


class User(BaseModel):
    name: str
    role: Role


def current_user(x_user: str = Header(..., min_length=1), x_role: str = Header(...)) -> User:
    try:
        return User(name=x_user.strip(), role=Role(x_role.lower()))
    except ValueError:
        raise HTTPException(401, f"X-Role must be one of: {', '.join(r.value for r in Role)}")


def require(*roles: Role):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, f"role '{user.role.value}' may not do this (needs: {', '.join(r.value for r in roles)})")
        return user
    return dep
