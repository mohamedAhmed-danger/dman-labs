from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from graph.prompts.booking_prompt import VISIT_SYSTEM_PROMPT
from graph.prompts.direct_prompt import DIRECT_SYSTEM_PROMPT
from graph.graph import route_agent_entry
from graph.nodes import region_guard_node
from models.models import Branch, Booking, City, Inquiry, LabService, Laboratory, db
from schemas.search import SearchResult
from search import db_queries, semantic_search
from search.fuzzy_search import fuzzy_search
from search.context_builder import ContextBuilder
from services.domain.branch_service import BranchService
from services.domain.booking_service import BookingService
from services.domain.inquiry_service import InquiryService
from services.messaging import client_service as client_service_module
from services.messaging.client_service import ClientService


@pytest.fixture
def catalog_app():
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        yield app


@pytest.fixture
def temporary_city_catalog(catalog_app):
    laboratory = Laboratory(
        name="Temporary catalog lab",
        location="Temporary location",
        description="Test fixture",
    )
    db.session.add(laboratory)
    db.session.flush()
    city_a = City(
        laboratory_id=laboratory.id,
        name="Test City A",
        aliases=["المدينه أ"],
        sort_order=1,
    )
    city_b = City(
        laboratory_id=laboratory.id,
        name="Test City B",
        sort_order=2,
    )
    db.session.add_all([city_a, city_b])
    db.session.flush()
    branch_a = Branch(
        city_id=city_a.id,
        name="City A Branch Only",
        address="A Street",
        phone_number="01012345678",
        working_hours="08:00-16:00",
    )
    branch_b = Branch(
        city_id=city_b.id,
        name="City B Branch Only",
        address="B Street",
        working_hours="10:00-18:00",
    )
    same_name_a = LabService(
        laboratory_id=laboratory.id,
        city_id=city_a.id,
        name="Shared Test",
        price=100.0,
        description="City A record",
    )
    same_name_b = LabService(
        laboratory_id=laboratory.id,
        city_id=city_b.id,
        name="Shared Test",
        price=200.0,
        description="City B record",
    )
    extra_a = LabService(
        laboratory_id=laboratory.id,
        city_id=city_a.id,
        name="Second Test",
        price=50.0,
        description="City A secondary record",
    )
    db.session.add_all([branch_a, branch_b, same_name_a, same_name_b, extra_a])
    db.session.commit()

    yield {
        "laboratory": laboratory,
        "city_a": city_a,
        "city_b": city_b,
        "same_name_a": same_name_a,
        "same_name_b": same_name_b,
        "extra_a": extra_a,
        "branch_a": branch_a,
        "branch_b": branch_b,
    }

    try:
        Branch.query.filter(
            Branch.city_id.in_([city_a.id, city_b.id])
        ).delete(synchronize_session=False)
        Booking.query.filter(Booking.city_id.in_([city_a.id, city_b.id])).delete(
            synchronize_session=False
        )
        Inquiry.query.filter(Inquiry.city_id.in_([city_a.id, city_b.id])).delete(
            synchronize_session=False
        )
        LabService.query.filter(
            LabService.laboratory_id == laboratory.id
        ).delete(synchronize_session=False)
        City.query.filter(City.laboratory_id == laboratory.id).delete(
            synchronize_session=False
        )
        db.session.delete(laboratory)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def test_region_guard_requires_region_even_for_nonregional_intents(monkeypatch):
    city = SimpleNamespace(id=7, laboratory_id=3, name="المدينة", aliases=[])
    query = Mock()
    query.filter_by.return_value = query
    query.order_by.return_value = query
    query.all.return_value = [city]
    monkeypatch.setattr(region_guard_node, "City", SimpleNamespace(query=query))
    monkeypatch.setattr(
        region_guard_node,
        "ClientService",
        Mock(return_value=Mock(get_city_id=lambda: None)),
    )
    monkeypatch.setattr(
        region_guard_node,
        "classify_region_message",
        lambda message, cities, current_city, previous_bot_message: region_guard_node.RegionDecision(
            action="ask_region",
            reply=f"اختار منطقتك:\n1. {cities[0].name}",
        ),
    )
    for intent, message in (
        ("complaint", "عندي شكوى"),
        ("direct", "السلام عليكم"),
        ("inquiry", "الباقات"),
        ("direct", ""),
    ):
        result = region_guard_node.region_guard_node({
            "laboratory_id": 3,
            "platform_id": 2,
            "page_id": "page",
            "sender_id": "sender",
            "intent": intent,
            "user_message": message,
        })
        assert "1. المدينة" in result["response"]
        assert result["region_guard_action"] == "stop"


def test_region_guard_skips_region_layer_for_client_with_saved_city(monkeypatch):
    city = SimpleNamespace(id=7, laboratory_id=3, name="المدينة", aliases=[])
    query = Mock()
    query.filter_by.return_value = query
    query.order_by.return_value = query
    query.all.return_value = [city]
    monkeypatch.setattr(region_guard_node, "City", SimpleNamespace(query=query))
    client_service = Mock()
    client_service.get_city_id.return_value = city.id
    monkeypatch.setattr(
        region_guard_node,
        "ClientService",
        Mock(return_value=client_service),
    )
    classify = Mock(return_value=region_guard_node.RegionDecision(
        action="ask_region",
        reply="",
    ))
    monkeypatch.setattr(region_guard_node, "classify_region_message", classify)

    result = region_guard_node.region_guard_node({
        "laboratory_id": 3,
        "platform_id": 2,
        "page_id": "page",
        "sender_id": "sender",
        "user_message": "عايز اغير المنطقة للقاهرة",
    })

    assert result["region_guard_action"] == "classify"
    assert result["city_id"] == city.id
    assert result["city_name"] == city.name
    client_service.update_city.assert_not_called()
    classify.assert_not_called()


def test_graph_routes_saved_city_directly_to_intent_classification():
    assert route_agent_entry({"city_id": 7}) == "intent"
    assert route_agent_entry({"city_id": None}) == "region_guard"


def test_region_selection_confirms_and_does_not_replay_question(monkeypatch):
    city = SimpleNamespace(id=7, laboratory_id=3, name="المدينة", aliases=[])
    query = Mock()
    query.filter_by.return_value = query
    query.order_by.return_value = query
    query.all.return_value = [city]
    monkeypatch.setattr(region_guard_node, "City", SimpleNamespace(query=query))
    client = Mock()
    client.get_city_id.return_value = None
    client.update_city.return_value = city
    monkeypatch.setattr(
        region_guard_node, "ClientService", Mock(return_value=client)
    )
    monkeypatch.setattr(
        region_guard_node,
        "classify_region_message",
        lambda message, cities, previous_bot_message: region_guard_node.RegionDecision(
            action="select_region",
            selected_city_number=1,
            reply="تم تحديد المنطقة، أرسل سؤالك مرة أخرى.",
        ),
    )
    result = region_guard_node.region_guard_node({
        "laboratory_id": 3,
        "platform_id": 2,
        "page_id": "page",
        "sender_id": "sender",
        "user_message": "1",
    })

    assert "user_message" not in result
    assert result["region_guard_action"] == "stop"
    assert result["city_id"] == 7
    client.update_city.assert_called_once_with(7)


def test_region_guard_uses_llm_reply_for_region_retries(monkeypatch):
    city = SimpleNamespace(id=7, laboratory_id=3, name="المدينة", aliases=[])
    query = Mock()
    query.filter_by.return_value = query
    query.order_by.return_value = query
    query.all.return_value = [city]
    monkeypatch.setattr(region_guard_node, "City", SimpleNamespace(query=query))
    monkeypatch.setattr(
        region_guard_node, "ClientService", Mock(return_value=Mock(get_city_id=lambda: None))
    )
    classify = Mock(side_effect=[
        region_guard_node.RegionDecision(
            action="ask_region",
            reply="ممكن تختار منطقتك من الاختيارات دي؟\n1. المدينة",
        ),
        region_guard_node.RegionDecision(
            action="ask_region",
            reply="لسه محتاج أعرف منطقتك عشان أقدر أساعدك.",
        ),
    ])
    monkeypatch.setattr(region_guard_node, "classify_region_message", classify)
    base_state = {
        "laboratory_id": 3,
        "platform_id": 2,
        "page_id": "page",
        "sender_id": "sender",
        "user_message": "اختيار غير صحيح",
    }

    retry = region_guard_node.region_guard_node(base_state)
    repeated = region_guard_node.region_guard_node(base_state)

    assert retry["response"] == "ممكن تختار منطقتك من الاختيارات دي؟\n1. المدينة"
    assert repeated["response"] == "لسه محتاج أعرف منطقتك عشان أقدر أساعدك."
    assert classify.call_count == 2
    assert retry["region_guard_action"] == "stop"
    assert not {"pending_question", "region_asked", "region_attempts"} & retry.keys()


def test_region_guard_uses_llm_to_interpret_initial_region_choice(monkeypatch):
    city = SimpleNamespace(id=7, name="المدينة", aliases=["مدينة النور"])
    calls = []

    class FakeLLM:
        def with_structured_output(self, schema):
            assert schema is region_guard_node.RegionDecision
            return self

        def invoke(self, messages):
            calls.append(messages)
            return region_guard_node.RegionDecision(
                action="ask_region",
                reply="اختار منطقتك الأول.",
            )

    monkeypatch.setattr(region_guard_node, "get_gemini", FakeLLM)
    decision = region_guard_node.classify_region_message(
        "القاهرة",
        [city],
    )
    assert decision.action == "ask_region"
    assert decision.reply == "اختار منطقتك الأول."
    assert calls
    assert "القاهرة" in calls[0][1].content
    assert "Do NOT classify intent" not in calls[0][0].content
    assert "correct branch and customer service team" in calls[0][0].content
    assert set(region_guard_node.RegionDecision.model_fields) == {
        "action",
        "selected_city_number",
        "reply",
    }


def test_direct_prompt_explains_region_choice():
    assert "what the difference is" in DIRECT_SYSTEM_PROMPT
    assert "correct branch and customer service team" in DIRECT_SYSTEM_PROMPT
    assert "unavailable in the current information" in DIRECT_SYSTEM_PROMPT


def test_booking_prompt_preserves_collected_fields_after_region_interruption():
    assert "ongoing booking record" in VISIT_SYSTEM_PROMPT
    assert "do not restart the booking" in VISIT_SYSTEM_PROMPT
    assert "Combine name parts supplied across consecutive messages" in VISIT_SYSTEM_PROMPT


def test_booking_prompt_collects_all_missing_fields_and_does_not_require_four_name_parts():
    assert "ask in one message for ALL required fields" in VISIT_SYSTEM_PROMPT
    assert "ask for all fields that remain missing in one concise" in VISIT_SYSTEM_PROMPT
    assert "do not require a particular" in VISIT_SYSTEM_PROMPT
    assert "at least 4 parts" not in VISIT_SYSTEM_PROMPT
    assert "tool until the request is understood" in VISIT_SYSTEM_PROMPT


def test_shared_prompt_pricing_has_no_discount_instructions():
    assert "DISCOUNT" not in VISIT_SYSTEM_PROMPT
    assert "الخصم" not in VISIT_SYSTEM_PROMPT
    assert "الإجمالي بعد الخصم" not in VISIT_SYSTEM_PROMPT


def test_region_guard_llm_answers_region_dependent_question_without_selecting_city(monkeypatch):
    city = SimpleNamespace(id=7, laboratory_id=3, name="المدينة", aliases=[])
    query = Mock()
    query.filter_by.return_value = query
    query.order_by.return_value = query
    query.all.return_value = [city]
    monkeypatch.setattr(region_guard_node, "City", SimpleNamespace(query=query))
    client_service = Mock()
    client_service.get_city_id.return_value = None
    monkeypatch.setattr(
        region_guard_node,
        "ClientService",
        Mock(return_value=client_service),
    )
    llm_reply = "ممكن تختلف التفاصيل حسب المنطقة. اختار منطقتك: 1. المدينة"
    decision = region_guard_node.RegionDecision(action="ask_region", reply=llm_reply)
    classify = Mock(return_value=decision)
    monkeypatch.setattr(region_guard_node, "classify_region_message", classify)

    result = region_guard_node.region_guard_node({
        "laboratory_id": 3,
        "platform_id": 2,
        "page_id": "page",
        "sender_id": "sender",
        "user_message": "هو الأسعار مختلفة حسب المنطقة؟",
    })

    assert result["response"] == llm_reply
    assert result["region_guard_action"] == "stop"
    assert "city_id" not in result
    classify.assert_called_once_with(
        "هو الأسعار مختلفة حسب المنطقة؟",
        [city],
        previous_bot_message=None,
    )
    client_service.update_city.assert_not_called()


def test_region_guard_rejects_llm_city_choice_outside_available_options(monkeypatch):
    city = SimpleNamespace(id=7, laboratory_id=3, name="المدينة", aliases=[])
    query = Mock()
    query.filter_by.return_value = query
    query.order_by.return_value = query
    query.all.return_value = [city]
    monkeypatch.setattr(region_guard_node, "City", SimpleNamespace(query=query))
    monkeypatch.setattr(region_guard_node, "send_production_alert", Mock())
    client_service = Mock()
    client_service.get_city_id.return_value = None
    monkeypatch.setattr(
        region_guard_node,
        "ClientService",
        Mock(return_value=client_service),
    )
    monkeypatch.setattr(
        region_guard_node,
        "classify_region_message",
        Mock(return_value=region_guard_node.RegionDecision(
            action="select_region",
            selected_city_number=2,
            reply="تم تغيير منطقتك.",
        )),
    )

    result = region_guard_node.region_guard_node({
        "laboratory_id": 3,
        "platform_id": 2,
        "page_id": "page",
        "sender_id": "sender",
        "user_message": "اختار المنطقة التانية",
    })

    assert result["region_guard_action"] == "stop"
    assert result["response"] == ""
    client_service.update_city.assert_not_called()


def test_prescription_without_city_is_not_saved_or_sent_to_ocr(monkeypatch):
    from services.messaging import prescription_intake

    monkeypatch.setattr(
        prescription_intake,
        "ClientService",
        Mock(return_value=Mock(get_or_create_client=lambda: SimpleNamespace(city_id=None))),
    )
    monkeypatch.setattr(
        prescription_intake,
        "_save_prescription_image",
        Mock(side_effect=AssertionError("image must not be saved before city selection")),
    )
    result = prescription_intake.extract_prescription_payload(
        b"\xff\xd8\xff" + b"0" * 20,
        SimpleNamespace(platform_id=2, page_id="page", sender_id="sender"),
        SimpleNamespace(laboratory_id=3),
    )
    assert result["mode"] == "immediate"
    assert "حدّد منطقتك" in result["reply"]
    prescription_intake._save_prescription_image.assert_not_called()


def test_city_service_rejects_cross_laboratory_city(monkeypatch):
    page = SimpleNamespace(laboratory_id=3)
    other_lab_city = SimpleNamespace(id=9, laboratory_id=4)
    page_query = Mock()
    page_query.filter_by.return_value.first.return_value = page
    monkeypatch.setattr(
        client_service_module, "Page", SimpleNamespace(query=page_query)
    )
    monkeypatch.setattr(
        client_service_module, "City", SimpleNamespace
    )
    session = Mock()
    session.get.return_value = other_lab_city
    monkeypatch.setattr(client_service_module, "db", SimpleNamespace(session=session))
    service = ClientService(platform_id=2, page_id="page", sender_id="sender")
    monkeypatch.setattr(service, "get_or_create_client", lambda: SimpleNamespace(city_id=None))
    with pytest.raises(ValueError, match="does not belong"):
        service.update_city(9)


@pytest.mark.mysql
def test_search_database_queries_are_city_scoped_and_keep_ranking(temporary_city_catalog):
    data = temporary_city_catalog
    lab_id = data["laboratory"].id
    city_a = data["city_a"].id
    city_b = data["city_b"].id

    a_candidates = db_queries.get_all_active_lab_services_for_search(lab_id, city_a)
    b_candidates = db_queries.get_all_active_lab_services_for_search(lab_id, city_b)
    assert {candidate["id"] for candidate in a_candidates} == {
        data["same_name_a"].id,
        data["extra_a"].id,
    }
    assert {candidate["id"] for candidate in b_candidates} == {data["same_name_b"].id}
    a_results = fuzzy_search(
        "Shared Test",
        lab_services=a_candidates,
        laboratory_id=lab_id,
        city_id=city_a,
    )
    b_results = fuzzy_search(
        "Shared Test",
        lab_services=b_candidates,
        laboratory_id=lab_id,
        city_id=city_b,
    )
    assert [result.id for result in a_results] == [data["same_name_a"].id]
    assert [result.id for result in b_results] == [data["same_name_b"].id]

    rows = db_queries.get_lab_services_by_ids(
        [data["extra_a"].id, 999999, data["same_name_a"].id, data["same_name_b"].id],
        lab_id,
        city_a,
    )
    assert [row.id for row in rows] == [
        data["extra_a"].id,
        data["same_name_a"].id,
    ]
    assert rows[0].name != rows[1].name


@pytest.mark.mysql
def test_context_uses_live_city_price_and_never_contains_other_city_price(temporary_city_catalog):
    data = temporary_city_catalog
    service_a = data["same_name_a"]
    service_b = data["same_name_b"]
    service_a.price = 123.45
    db.session.commit()

    result = SearchResult(
        id=service_a.id,
        name=service_a.name,
        score=0.91,
        source="fuzzy",
    )
    context = ContextBuilder.format_search_results_to_context(
        [result],
        {service_a.id: service_a},
        data["laboratory"].id,
        data["city_a"].id,
    )
    assert "123.45 EGP" in context
    assert "200.00 EGP" not in context

    no_match = ContextBuilder.format_search_results_to_context(
        [],
        {},
        data["laboratory"].id,
        data["city_a"].id,
    )
    assert "No matching test in the client's region" in no_match

    rejected_foreign_row = ContextBuilder.format_search_results_to_context(
        [SearchResult(id=service_b.id, name=service_b.name, score=0.9, source="semantic")],
        {service_b.id: service_b},
        data["laboratory"].id,
        data["city_a"].id,
    )
    assert "200.00 EGP" not in rejected_foreign_row
    assert "No matching test in the client's region" in rejected_foreign_row


@pytest.mark.mysql
def test_rag_context_never_contains_branch_details(temporary_city_catalog, monkeypatch):
    from search import context_builder

    data = temporary_city_catalog
    service = data["same_name_a"]
    result = SearchResult(
        id=service.id,
        name=service.name,
        score=0.9,
        source="fuzzy",
    )
    monkeypatch.setattr(
        context_builder,
        "run_search",
        lambda *args: {"results": [result], "top_score": result.score},
    )
    monkeypatch.setattr(
        context_builder,
        "get_lab_services_by_ids",
        lambda ids, laboratory_id, city_id: [service],
    )
    context, _, _ = ContextBuilder.build_rag_context(
        [{
            "query": "Shared Test",
            "aliases": [],
            "keywords": [],
            "description": "A laboratory test",
        }],
        data["laboratory"].id,
        data["city_a"].id,
    )
    assert "City A Branch Only" not in context
    assert "City B Branch Only" not in context


@pytest.mark.mysql
@pytest.mark.parametrize("node_name", ["inquiry_node", "booking_node", "direct_node"])
def test_city_branches_are_injected_only_for_the_selected_city(
    node_name,
    temporary_city_catalog,
    monkeypatch,
):
    from importlib import import_module

    data = temporary_city_catalog
    node_module = import_module(f"graph.nodes.{node_name}")
    prompts = []
    chain = SimpleNamespace(
        invoke=lambda messages: (
            prompts.append(messages[0].content)
            or (
                SimpleNamespace(tool_calls=[], content="ok")
                if node_name == "booking_node"
                else {"parsed": SimpleNamespace(reply="ok", summary=""), "raw": None}
            )
        )
    )
    llm = Mock()
    if node_name == "booking_node":
        llm.bind_tools.return_value = chain
    else:
        llm.with_structured_output.return_value = chain
    monkeypatch.setattr(node_module, "get_gemini", lambda: llm)
    monkeypatch.setattr(node_module, "extract_token_usage", lambda _: {})
    monkeypatch.setattr(
        node_module.LaboratoryService,
        "get_current_lab_info",
        lambda laboratory_id: ("Test Lab", "Test Location", "Test Description"),
    )
    node = getattr(node_module, node_name)
    node({
        "page_id": "page",
        "sender_id": "sender",
        "platform_id": 2,
        "laboratory_id": data["laboratory"].id,
        "city_id": data["city_a"].id,
        "user_message": "hello",
        "chat_history": [],
    })
    assert len(prompts) == 1
    assert "City A Branch Only" in prompts[0]
    assert "A Street" in prompts[0]
    assert "01012345678" in prompts[0]
    assert "08:00-16:00" in prompts[0]
    assert "City B Branch Only" not in prompts[0]
    assert "B Street" not in prompts[0]
    assert BranchService.get_city_phone_numbers(
        data["laboratory"].id,
        data["city_a"].id,
    ) == ["01012345678"]


@pytest.mark.mysql
def test_branch_context_reports_empty_city_without_other_city_rows(temporary_city_catalog):
    data = temporary_city_catalog
    city = City(
        laboratory_id=data["laboratory"].id,
        name="Empty Branch City",
        sort_order=3,
    )
    db.session.add(city)
    db.session.flush()
    context = BranchService.format_city_branches_context(
        data["laboratory"].id,
        city.id,
    )
    assert "Empty Branch City" in context
    assert "No branches or branch details are available" in context
    assert "City A Branch Only" not in context
    assert BranchService.get_city_phone_numbers(data["laboratory"].id, city.id) == []


def test_semantic_search_uses_city_collection_and_payload_filter(monkeypatch):
    captured = {}

    class FakeQdrant:
        def query_points(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(points=[])

    monkeypatch.setattr(semantic_search, "get_qdrant_client", lambda: FakeQdrant())
    semantic_search._query_qdrant([0.1, 0.2], 3, laboratory_id=12, city_id=34)

    assert captured["collection_name"] == "tests_lab12_city34"
    conditions = captured["query_filter"].must
    assert {(item.key, item.match.value) for item in conditions} == {
        ("laboratory_id", 12),
        ("city_id", 34),
    }


@pytest.mark.mysql
def test_booking_and_inquiry_persist_the_selected_city(temporary_city_catalog, monkeypatch):
    data = temporary_city_catalog
    from services.domain import booking_service as booking_service_module

    monkeypatch.setattr(
        booking_service_module,
        "generate_booking_img",
        lambda **kwargs: b"ticket",
    )
    booking_result = BookingService().save_booking(
        name="Patient Test",
        phone="01000000000",
        date="2030-01-01",
        details="Shared Test",
        comes_from="WhatsApp",
        time="12:00",
        address="Test address",
        city_id=data["city_a"].id,
    )
    inquiry_result = InquiryService().save_inquiry(
        laboratory_id=data["laboratory"].id,
        city_id=data["city_a"].id,
        phone_number="",
        comes_from="whatsapp:sender:page",
        services_mentioned="Shared Test",
    )

    assert db.session.get(Booking, booking_result.booking_id).city_id == data["city_a"].id
    assert inquiry_result.success
    assert inquiry_result.inquiry.city_id == data["city_a"].id


def test_staff_templates_hide_missing_city_labels():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    inquiry_template = (root / "templates/inquiries/list.html").read_text()
    inquiry_detail = (root / "templates/inquiries/detail.html").read_text()
    booking_template = (root / "templates/bookings/list.html").read_text()
    booking_detail = (root / "templates/bookings/detail.html").read_text()
    assert "{% if inq.city %}" in inquiry_template
    assert "{% if inquiry.city %}" in inquiry_detail
    assert "{% if b.city %}" in booking_template
    assert "{% if booking.city %}" in booking_detail
