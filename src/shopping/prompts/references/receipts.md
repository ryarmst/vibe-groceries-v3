---
title: Reading grocery receipts
description: Looked up by the assistant (get_reference) when decoding a pasted grocery receipt.
---
# Reading grocery receipts

The household shops mostly at Canadian grocers (Loblaw banners such as No Frills, Zehrs, and Real Canadian Superstore; also Costco, Metro, and Food Basics). Receipts are terse; decode carefully and prefer "Unclear" over a confident wrong guess.

## Line structure

- **Item lines**: abbreviated description, sometimes a product code, then price. Long names are truncated at a fixed width.
- **Weighed items**: the item line is followed by a line like `0.765 kg @ $4.39/kg`. Treat both lines as one item.
- **Multiples**: `2 @ $3.49` or a quantity prefix; still one row in Notion.
- **Discounts and coupons**: lines with negative amounts, "PC OPTIMUM", "SAVINGS", "MULTI", or "PRICE MATCH" belong to the item above; ignore them.
- **Deposits and fees**: "DEPOSIT", "ENV FEE", "BAG" lines are not items.
- **Department headers** on some receipts (PRODUCE, MEATS, DAIRY, GROCERY, FROZEN) help classify the lines beneath them.
- **Purchase date**: usually near the top or bottom with the time and store number. Use it for `Expires` and `Purchased`.

## Common abbreviations

| Abbreviation | Meaning |
|---|---|
| PC | President's Choice (store brand) |
| NN | No Name (store brand) |
| ORG, PCO | Organic, PC Organics |
| BNLS, SKNLS | Boneless, skinless |
| CHKN, BF, PRK | Chicken, beef, pork |
| GRND, LN, XLN | Ground, lean, extra lean |
| THGH, BRST, DRMSTK | Thigh, breast, drumstick |
| VEG, FRT | Vegetable, fruit |
| GRK YGRT | Greek yogurt |
| 2% / 1% / SKM / HOMO | Milk fat (HOMO = homogenized whole milk) |
| BTR | Butter |
| CHS, CHED, MOZZ | Cheese, cheddar, mozzarella |
| SPNCH, BRCLI, CAULI | Spinach, broccoli, cauliflower |
| GRN ONION, RD ONION | Green onion, red onion |
| WW, MG | Whole wheat, multigrain |

Four- or five-digit numbers on produce lines are PLU codes (a leading 9 on a five-digit PLU means organic). They confirm the line is produce but rarely identify it beyond the description.

## Ontario HST as a classification hint

Basic groceries are zero-rated for HST in Ontario. Receipts flag taxable lines with a code (often H, HST, or a letter defined in the tax summary at the bottom). Taxable lines are usually snacks, candy, soft drinks, prepared food, and household or personal-care goods, which this skill skips. Use this as a hint, not a rule: an existing Notion row (for example Chips and Snacks or Ziplock Bags) is still updated, and tax codes vary by chain.

## What to track

Track, perishable: produce, meat and fish, dairy and eggs, bread, tofu, fresh pasta, refrigerated dips and ferments.

Track, staple: rice and grains, flour, sugar, oils, vinegars, dried pasta, canned tomatoes, beans and lentils, stock and bouillon, core spices, and condiments he restocks.

Skip: snacks, candy, drinks, ready-to-eat and prepared items, frozen convenience food, household and personal-care goods, and one-off non-perishables, unless the line matches an existing Notion row.
