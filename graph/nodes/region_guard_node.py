import logging
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from graph.state import AgentState
from models.models import City
from llm.llm import get_gemini
from notification_center import send_production_alert
from services.messaging.client_service import ClientService
from utils.history_utils import get_chat_history
from utils.text_utils import detect_language_fallback

logger = logging.getLogger(__name__)

# After the bot has already asked for the region this many times without a
# valid selection, it must stop repeating the list and hand over to support.
MAX_REGION_ASKS = 2

# The single Customer Service number given to a customer who is stuck on the
# region question. Fill it in, e.g. "01012345678". While it is empty, no phone
# line is added to the reply (a number is never invented).
REGION_SUPPORT_PHONE = ""


class RegionDecision(BaseModel):
    action: Literal["ask_region", "select_region", "escalate"] = Field(
        description=(
            "ask_region: ask for / re-ask for a region. "
            "select_region: save the customer's explicitly selected region. "
            "escalate: the customer cannot or does not want to choose a listed "
            "region (not in any of them, confused, upset, or asked repeatedly)."
        )
    )
    selected_city_number: int | None = Field(
        default=None,
        description="The 1-based number of the explicitly selected available region.",
    )
    reply: str = Field(description="A polite, professional reply to send to the customer.")


def _count_previous_region_asks(recent_history: str, cities: list[City]) -> int:
    """Approximate how many times the bot already showed the region list.

    Every region-ask contains the numbered list, so the first option line
    ("1. <first city>") appears once per ask in the recent history.
    """
    if not cities or not recent_history:
        return 0
    return recent_history.count(f"1. {cities[0].name}")


def _stuck_fallback_reply(cities: list[City]) -> str:
    """Fixed reply used when the customer is stuck in the region question."""
    names = "، ".join(city.name for city in cities)
    return (
        "نعتذر عن الإزعاج فندم. المناطق التي نخدمها حالياً هي: "
        f"{names}.\n\n"
        "إذا كنت خارج هذه المناطق أو تحتاج مساعدة إضافية، "
        "برجاء التواصل مع خدمة العملاء هاتفياً."
    )


def _support_phone_line(language_hint: str) -> str:
    """Customer Service phone line, in the customer's language.

    Empty when REGION_SUPPORT_PHONE is not set.
    """
    phone = (REGION_SUPPORT_PHONE or "").strip()
    if not phone:
        return ""
    return detect_language_fallback(
        language_hint,
        arabic=(
            "\n\nللتواصل مع خدمة العملاء هاتفياً:\n"
            f"📞 *{phone}*"
        ),
        default=(
            "\n\nTo contact Customer Service by phone:\n"
            f"📞 *{phone}*"
        ),
    )


def classify_region_message(
    message: str,
    cities: list[City],
    recent_history: str = "",
    previous_bot_message: str | None = None,
    previous_ask_count: int = 0,
) -> RegionDecision | None:
    """Use the LLM to interpret region intent; the application validates any choice."""
    options = "\n".join(
        f"{index}. {city.name}"
        + (
            f" (also known as: {', '.join(city.aliases)})"
            if isinstance(city.aliases, list) and city.aliases
            else ""
        )
        for index, city in enumerate(cities, 1)
    ) or "(No regions are configured.)"

    previous_reply = previous_bot_message or "None"

    system_prompt = f"""
You are the intelligent AI customer service assistant for
Al-Daman Laboratories (معامل ضمان).

Your official identity is:
"المساعد الذكي لمعامل ضمان"

You are professional, polite, and courteous.
Reply in the customer's own language and dialect (for example Egyptian or Gulf
Arabic, or English), using simple, formal, respectful wording.

You are chatting with a customer who does not have a saved region yet.
Interpret their message and return one structured decision:
- ask_region: the customer has not clearly chosen a region yet.
- select_region: the customer clearly chose one of the available regions.
- escalate: the customer cannot or does not want to choose one of the
  available regions (see ESCALATION RULES).

IDENTITY RULES:
- Introduce yourself as "المساعد الذكي لمعامل ضمان" in your FIRST reply to the
  customer. Example:
  "أهلاً بحضرتك، أنا المساعد الذكي لمعامل ضمان، موجود لمساعدة حضرتك في
  استفسارات التحاليل وحجز الزيارات المنزلية."
- Introduce yourself ONLY ONCE. If the customer has already received an
  introduction, do not introduce yourself again.
- If the customer asks who you are, answer honestly.
- Never claim to be a human employee.
- Keep the introduction short.

Available regions, in the exact order shown to the customer:
{options}

RECENT CONVERSATION (oldest first; the current message is NOT included yet):
{recent_history or "None"}

LAST ASSISTANT MESSAGE:
{previous_reply}

TIMES THE REGION LIST WAS ALREADY SHOWN TO THIS CUSTOMER: {previous_ask_count}

DECISION RULES
- Select a region only when the customer clearly chooses one of the available
  regions after being asked for it. A region name, a spelling variant or
  alias (e.g. "كايرو" for Cairo), or an option number can be a selection in
  that context.
- For select_region, set selected_city_number to the 1-based number from the
  list. Never invent a region or number.
- If the region list is empty, do not claim a region was selected or ask the
  customer to choose from an empty list. Explain politely that no regions are
  currently available and ask them to contact the laboratory through this chat.

ESCALATION RULES (CRITICAL: NEVER LOOP)
- Never ask for the region more than {MAX_REGION_ASKS} times. If the list was
  already shown {MAX_REGION_ASKS} times or more, you MUST NOT return ask_region.
  Return select_region (only if the customer clearly chose) or escalate.
- Return escalate immediately, even on the first ask, when the customer:
  * says or implies he is not in any of the listed regions, or names a city or
    country that is not in the list;
  * does not understand why the region is needed and the explanation did not
    help;
  * is confused, upset, or complaining about the question (for example calls
    the question nonsense or says the bot is not understanding).
- For escalate, the reply must:
  * start with ONE short, sincere apology or acknowledgment;
  * NOT repeat the numbered list. If useful, mention the region names inline
    in one short sentence, as the areas the laboratory currently serves;
  * say that if he is outside these areas or needs further help, he should
    contact Customer Support BY PHONE (هاتفياً);
  * NOT write any phone number yourself. The system adds the Customer Service
    number(s) right after your reply. Never invent a number.

REPLY STYLE
- Be formal, polite, and professional. Address the customer with "فندم" or
  "حضرتك". NEVER call the customer by his name. No slang, no jokes, and no
  decorative emojis.
- Never repeat the last assistant message word for word. Vary the wording.
- Keep it short: one or two sentences plus the options when asking.
- Do not invent any test, price, or branch information.
- Never argue with or respond to an insult in kind. Stay calm and respectful.

For ask_region:
- Use it only while the list was shown fewer than {MAX_REGION_ASKS} times and
  the customer is not confused or upset.
- If the customer asked why a region is needed, answer briefly and honestly:
  it routes them to the correct branch and customer service team. Then ask
  them to choose.
- If the customer already asked about something (a test, a price, a booking),
  mention that topic naturally and explain you need the region first to help
  with it.
- If the message is unclear or is not one of the available options, politely
  say these are the areas the laboratory serves, and ask again.
- When you ask, list the available options exactly as numbered above.

For select_region:
- Confirm the region in one short line.
- If the customer asked something earlier in the conversation, mention that
  topic and ask them to send it again so you can answer with the right branch
  details. If they never asked anything, ask how you can help.
"""
    try:
        structured_llm = get_gemini().with_structured_output(RegionDecision)
        return structured_llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=message),
        ])
    except Exception as exc:
        logger.exception("[REGION GUARD] LLM region decision failed")
        send_production_alert(
            subject="Region Guard LLM Failure",
            body_or_error=exc,
            context={"available_city_count": len(cities)},
        )
        return None


def _guard_error(message: str, **context) -> dict:
    logger.error("[REGION GUARD] %s | context=%s", message, context)
    send_production_alert(
        subject="Region Guard Could Not Process Message",
        body_or_error=message,
        context=context,
    )
    return {"response": "", "region_guard_action": "stop"}


def region_guard_node(state: AgentState) -> dict:
    """Skip region selection for clients who already have a saved city."""
    laboratory_id = state.get("laboratory_id")
    if not laboratory_id:
        return _guard_error("Missing laboratory_id")

    client_service = ClientService(
        platform_id=state.get("platform_id"),
        page_id=state.get("page_id"),
        sender_id=state.get("sender_id"),
    )
    cities = City.query.filter_by(laboratory_id=laboratory_id).order_by(
        City.sort_order.asc(), City.id.asc()
    ).all()
    current_city_id = client_service.get_city_id()
    current_city = None
    if current_city_id is not None:
        current_city = next((city for city in cities if city.id == current_city_id), None)
        if not current_city:
            return _guard_error(
                "Client city is not in page laboratory",
                city_id=current_city_id,
                laboratory_id=laboratory_id,
            )

    if current_city:
        return {
            "city_id": current_city.id,
            "city_name": current_city.name,
            "region_guard_action": "classify",
        }

    _, recent_history = get_chat_history(state, client_service)
    ask_count = _count_previous_region_asks(recent_history, cities)

    decision = classify_region_message(
        state.get("user_message", ""),
        cities,
        recent_history=recent_history,
        previous_bot_message=state.get("last_bot_message"),
        previous_ask_count=ask_count,
    )
    if decision is None:
        return {"response": "", "region_guard_action": "stop"}

    action = decision.action
    reply = (decision.reply or "").strip()

    # Safety net: never let the customer loop on the region question, even if
    # the LLM ignored the escalation rules.
    if action == "ask_region" and cities and ask_count >= MAX_REGION_ASKS:
        logger.warning(
            "[REGION GUARD] Region asked %s times without selection; "
            "forcing escalation reply | sender_id=%s",
            ask_count,
            state.get("sender_id"),
        )
        action = "escalate"
        reply = _stuck_fallback_reply(cities)

    if not reply:
        return _guard_error(
            "LLM region decision returned an empty customer reply",
            action=action,
        )

    if action == "select_region":
        selected_number = decision.selected_city_number
        if selected_number is None or not 1 <= selected_number <= len(cities):
            return _guard_error(
                "LLM selected a region outside the available options",
                selected_city_number=selected_number,
                available_city_count=len(cities),
            )
        target_city = cities[selected_number - 1]
        try:
            client_service.update_city(target_city.id)
        except ValueError:
            logger.exception("[REGION GUARD] Rejected initial city selection | city_id=%s", target_city.id)
            return _guard_error(
                "Selected region does not belong to the client's laboratory",
                city_id=target_city.id,
                laboratory_id=laboratory_id,
            )
        return {
            "city_id": target_city.id,
            "city_name": target_city.name,
            "response": reply,
            "last_bot_message": reply,
            "region_guard_action": "stop",
        }

    if action == "escalate":
        logger.warning(
            "[REGION GUARD] Customer could not choose a region | sender_id=%s ask_count=%s",
            state.get("sender_id"),
            ask_count,
        )
        language_hint = state.get("user_message", "")
        if not any(character.isalpha() for character in language_hint):
            language_hint = state.get("last_bot_message") or language_hint
        reply += _support_phone_line(language_hint)

    return {
        "response": reply,
        "last_bot_message": reply,
        "region_guard_action": "stop",
    }