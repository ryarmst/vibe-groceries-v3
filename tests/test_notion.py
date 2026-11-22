import httpx
import pytest

from fake_notion import COOKING, GROCERIES, MEALS, PANTRY, FakeNotion
from shopping.notion import kitchen as kmod
from shopping.notion.blocks import blocks_to_text, text_to_blocks
from shopping.notion.client import NotionClient, clean_id
from shopping.notion.kitchen import Kitchen, norm
from shopping.notion.props import write

T = lambda s: [{"type": "text", "text": {"content": s}}]  # noqa: E731


@pytest.fixture
def notion():
    kmod._schema_cache.clear()
    kmod._rows_cache.clear()
    fake = FakeNotion()
    client = NotionClient("secret", transport=httpx.MockTransport(fake.handler))
    client.MIN_INTERVAL = 0
    return fake, Kitchen(client, {"pantry": PANTRY, "groceries": GROCERIES, "cooking": COOKING, "meals": MEALS})


def test_ids_and_names():
    assert clean_id("collection://0a1b2c3d-1111-4222-8333-444455556666") == "0a1b2c3d-1111-4222-8333-444455556666"
    assert clean_id("https://app.notion.com/p/Cooking-40c2d1e0e40080a1a990dfbacef23669") == "40c2d1e0-e400-80a1-a990-dfbacef23669"
    assert norm("<p>Chicken Thighs</p>") == norm("chicken thigh") and norm("Glass") == "glass"


def test_property_writes_follow_schema():
    schema = {"Name": {"type": "title"}, "Tags": {"type": "multi_select"}, "Done": {"type": "checkbox"}, "Total": {"type": "formula"}}
    payload, skipped = write(schema, {"Name": "x" * 2500, "Tags": "Bulk, Braise", "Done": 1, "Total": 3, "Missing": 1})
    assert len(payload["Name"]["title"]) == 2 and payload["Tags"]["multi_select"] == [{"name": "Bulk"}, {"name": "Braise"}]
    assert payload["Done"] == {"checkbox": True} and set(skipped) == {"Total", "Missing"}


def test_blocks_round_trip():
    md = "📌 **Why:** pork is $6.61/kg\n## Ingredients\n- 3 lb (1.4 kg) pork picnic\n## Process\n1. **Sear** 10 min\n2. Braise\n- [x] done\n---\nPlain para."
    blocks = text_to_blocks(md)
    assert blocks[0]["type"] == "callout" and blocks[0]["callout"]["rich_text"][0]["annotations"]["bold"]
    assert blocks_to_text(blocks) == md


async def test_grocery_upsert_dedupes_and_links_pantry(notion):
    fake, k = notion
    eggs = fake.add(PANTRY, {"Name": T("Eggs"), "Type": {"name": "Staple"}})
    fake.add(GROCERIES, {"Name": T("Milk"), "Quantity": T("1"), "Status": {"name": "New"}})
    created = await k.grocery_upsert("eggs", "2 dozen")
    assert created["linked_pantry"] and fake.pages[created["created"]]["properties"]["🥕 Kitchen Pantry and Storage"]["relation"] == [{"id": eggs}]
    again = await k.grocery_upsert("Egg", "18")  # same item, different spelling: updates, no duplicate
    assert again["updated"] == created["created"]
    assert sum(1 for p in fake.pages.values() if p["ds"] == GROCERIES) == 2
    assert (await k.grocery_upsert("Milk", None))["updated"]


async def test_pantry_rules(notion):
    fake, k = notion
    fake.add(PANTRY, {"Name": T("<p>Pecans</p>")})
    with pytest.raises(Exception, match="already exists"):
        await k.pantry_create("pecan", {})
    rows = await k.pantry()
    assert rows[0]["Name"] == "Pecans"
    with pytest.raises(Exception, match="not writable"):
        await k.pantry_update(rows[0]["id"], {"Name": "Walnuts"})


async def test_promote_meal_copies_to_cooking(notion):
    fake, k = notion
    body = text_to_blocks("📌 Weeknight braise\n## Ingredients\n- pork")
    meal = fake.add(MEALS, {"Name": T("Carnitas"), "Status": {"name": "Cooked"}, "Tags": [{"name": "Braise"}], "Effort": {"name": "Easy"},
                            "Cuisine": [{"name": "Mexican"}],
                            "Shopping Ingredients": T("Serves: 6\nPork Shoulder | 3 lb")}, body)
    out = await k.promote_meal(meal)
    page = fake.pages[out["cooking_page"]]
    assert page["ds"] == COOKING and page["properties"]["Tags"]["multi_select"] == [{"name": "Braise"}]
    assert page["properties"]["Effort"]["select"] == {"name": "Easy"}
    assert page["properties"]["Cuisine"]["rich_text"][0]["text"]["content"] == "Mexican"  # multi-select → Cooking's text
    assert fake.blocks[out["cooking_page"]][0]["type"] == "callout"
    props = fake.pages[meal]["properties"]
    assert props["Status"]["status"]["name"] == "Promoted" and props["Promoted To"]["relation"] == [{"id": out["cooking_page"]}]
    assert "Based On" not in props  # the other Cooking relation is untouched
    assert (await k.promote_meal(meal)).get("already_promoted")


async def test_unconfigured_meals_database(notion):
    _, k = notion
    k.ids["meals"] = ""
    with pytest.raises(Exception, match="Suggested Meals database isn't configured"):
        await k.meals()


async def test_database_id_resolves_to_its_data_source(notion):
    fake, k = notion
    database_id = "41f2d1e0-e400-80cb-929e-df6ba145a2c4"
    fake.databases[database_id] = MEALS
    k.ids["meals"] = database_id  # what "Copy link" gives
    created = await k.meal_create({"Name": "Dal", "Status": "Suggested", "Cuisine": ["Indian", "Nepali"]}, "📌 x")
    assert fake.pages[created["created"]]["ds"] == MEALS
    assert k.ids["meals"] == MEALS and len(await k.meals()) == 1
