"""Ask Guard, the dashboard assistant (app/ai/assistant.py): one chat answer per request.

The browser keeps the conversation and sends it back each time (text only), so nothing about a chat is stored
here. Changes come back as proposals; the dashboard sends each one, on Confirm, to the endpoint it names.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import assistant
from app.api.deps import Member, OwnerShop
from app.api.ratelimit import limit
from app.config import Settings, get_settings
from app.db.session import get_session

router = APIRouter(prefix="/assistant", tags=["assistant"])


class Message(BaseModel):
    role: Literal["user", "assistant", "note"]
    text: str = Field(min_length=1, max_length=4000)


class ChatIn(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=40)
    # The dashboard page the member has open (e.g. /guard/incidents/abc), so "this incident" means something.
    page: str | None = Field(default=None, max_length=200, pattern=r"^/[\w/\-#]*$")

    @field_validator("messages")
    @classmethod
    def _ends_with_question(cls, v: list[Message]) -> list[Message]:
        if v[-1].role != "user":
            raise ValueError("the last message must be the member's")
        return v


@router.post("/chat", dependencies=[Depends(limit("assistant", 20, 60))])
async def chat(
    body: ChatIn,
    shop_id: OwnerShop,
    user: Member,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    ctx = assistant.Ctx(session=session, shop_id=shop_id, user=user, settings=settings, page=body.page)
    try:
        return await assistant.chat(ctx, [m.model_dump() for m in body.messages])
    except assistant.AssistantError as exc:
        raise HTTPException(exc.status, exc.message) from exc
