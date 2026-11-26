import pytest

from shopping import prompts
from shopping.agent import deal_review, playbooks
from shopping.database import session_scope
from test_api import client, signed_in  # noqa: F401 - fixtures
from test_api import access  # noqa: F401


@pytest.fixture(autouse=True)
def clean():
    yield
    with session_scope() as s:
        for pid in prompts.catalog():
            prompts.reset(s, pid)


def test_every_prompt_file_parses():
    cat = prompts.catalog()
    assert set(cat) == set(prompts.ORDER)
    for p in cat.values():
        assert p.title and p.description and p.group
        assert p.id == "about" or p.default
        assert not prompts.unknown_placeholders(p.id, p.default), p.id
    assert cat["tasks/quick"].tools and cat["tasks/quick"].tier == "fast"
    assert "shopping_quick_add" in cat["tasks/quick"].tools


def test_render_leaves_json_alone():
    out = prompts.render('{household} likes {"picks": []} and {unknown}', {"household": "Alex"})
    assert out == 'Alex likes {"picks": []} and {unknown}'


def test_override_feeds_the_agent_but_tools_stay_fixed():
    with session_scope() as s:
        warnings = prompts.save(s, "tasks/quick", body="Be terse. {nonsense}", tier="smart", by="Alex")
    assert warnings == ["nonsense"]
    pb = playbooks.load("quick")
    assert pb.body == "Be terse. {nonsense}" and pb.tier == "smart"
    assert pb.tools == prompts.get("tasks/quick").tools  # editing text can't grant or remove tools
    with session_scope() as s:
        prompts.reset(s, "tasks/quick", by="Alex")
        assert [r.note for r in prompts.revisions(s, "tasks/quick")] == ["before reset", "before edit"]
    assert playbooks.load("quick").body == prompts.get("tasks/quick").default


def test_about_is_added_everywhere_and_can_be_empty():
    assert "About this household" not in playbooks.common()
    with session_scope() as s:
        prompts.save(s, "about", body="We don't eat pork. We have a pressure cooker.")
    assert "We don't eat pork" in playbooks.common()
    system, tier = deal_review.system_prompt()
    assert "We don't eat pork" in system and '{"picks"' in system and tier == "fast"
    with session_scope() as s:
        prompts.save(s, "about", body="")
    assert "About this household" not in playbooks.common()


def test_validation():
    with session_scope() as s:
        with pytest.raises(prompts.PromptError):
            prompts.save(s, "tasks/shop", body="   ")
        with pytest.raises(prompts.PromptError):
            prompts.save(s, "household", body="x", tier="smart")  # household has no model of its own
        with pytest.raises(prompts.PromptError):
            prompts.save(s, "tasks/shop", body="x" * (prompts.MAX_CHARS + 1))
        with pytest.raises(prompts.PromptError):
            prompts.get("../../etc/passwd")
        # Saving the default text again removes the override instead of storing a copy.
        prompts.save(s, "tasks/ask", body="changed")
        prompts.save(s, "tasks/ask", body=prompts.get("tasks/ask").default)
        assert prompts.current(s, "tasks/ask")[2] is None


def test_revisions_are_capped():
    with session_scope() as s:
        for i in range(prompts.KEEP_REVISIONS + 5):
            prompts.save(s, "references/receipts", body=f"v{i}")
        assert len(prompts.revisions(s, "references/receipts")) == prompts.KEEP_REVISIONS


def test_prompts_api(client):  # noqa: F811
    h = signed_in()
    listing = client.get("/api/prompts", headers=h).json()
    assert [p["id"] for p in listing["prompts"]] == prompts.ORDER and "Chat tasks" in listing["groups"]
    d = client.get("/api/prompts/tasks/shop", headers=h).json()
    assert d["body"] == d["default"] and d["tools"] and not d["customized"]
    r = client.put("/api/prompts/tasks/shop", json={"body": "Shop well for {household}."}, headers=h)
    assert r.status_code == 200 and r.json()["customized"] and r.json()["updated_by"] == "Alex"
    pv = client.get("/api/prompts/tasks/shop/preview", headers=h).json()
    assert "Shop well for a household" in pv["text"] and "{household}" not in pv["text"] and "## Mode: Plan a shop" in pv["text"]
    rev = r.json()["revisions"][0]
    old = client.get(f"/api/prompts/tasks/shop/revisions/{rev['id']}", headers=h).json()
    assert old["body"] == prompts.get("tasks/shop").default
    assert client.put("/api/prompts/tasks/shop", json={"body": ""}, headers=h).status_code == 400
    assert client.put("/api/prompts/tasks/shop", json={"body": "x"}).status_code == 401
    assert client.put("/api/prompts/tasks/shop", json={"body": "x"},
                      headers={"cf-access-jwt-assertion": h["cf-access-jwt-assertion"]}).status_code == 403  # no app header
    assert not client.delete("/api/prompts/tasks/shop", headers=h).json()["customized"]
    assert client.get("/api/prompts/nope", headers=h).status_code == 404
    assert client.get("/api/prompts/references/receipts/preview", headers=h).status_code == 400


def test_plan_my_week_task():
    pb = playbooks.load("week")
    assert pb.steps == 16 and pb.tier == "smart" and "week" in playbooks.MODES
    assert {"meals_context", "meal_suggest", "shopping_stage_items", "shopping_get_requirement_history"} <= set(pb.tools)
    assert "shopping_quick_add" not in pb.tools  # it builds a run for review, never fills the cart directly
    assert playbooks.route("plan my week") != "week"  # chosen by the button or the task picker, not guessed
