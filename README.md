# MTG Art Picker — Magic: The Gathering Art Downloader

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Release](https://img.shields.io/github/v/release/sk90y/mtg-art-picker)](https://github.com/sk90y/mtg-art-picker/releases)
[![Downloads](https://img.shields.io/github/downloads/sk90y/mtg-art-picker/total)](https://github.com/sk90y/mtg-art-picker/releases)

MTG Art Picker is a fast desktop tool for Magic: The Gathering players to browse, compare, and download card artwork using the **Scryfall API**.  
Perfect for proxy printing, deck customization, cube design, or just collecting your favorite art.

Built with PySide6 and powered by Scryfall.

## Features
- 🖼️ Browse **every printing** of any Magic card
- 🔍 Filter by border, frame, art style, and more
- ⌨️ Full keyboard navigation for fast selection
- 💾 Local cache – works offline after first load
- 📥 Batch download selected images (PNG/JPG)
- ↔️ Compare all versions side‑by‑side
- 🌐 English/中文 UI language support
- 🧩 Token query helper (supports shorthand like `Human 1/1`)

## What’s New in v2.0.4
- ✅ Fixed startup crash (`MainWindow` initialization order issue)
- ✅ Added token query guide under the token input box
- ✅ Token shorthand normalization:
  - `Human 1/1` → `type:token Human pow=1 tou=1`
  - `cat 2/2` → `type:token cat pow=2 tou=2`
  - `treasure` → `type:token treasure`
- ✅ Added language selector on the first screen
- ✅ First-screen language label now intentionally flips to make switching easier:
  - English UI: `语言：English`
  - Chinese UI: `Language: 中文`

Perfect for:

- Proxy printing
- Deck customization
- Collectors choosing favorite artwork
- Cube design
- MTG enthusiasts

Built with PySide6 and powered by Scryfall.

---

## 🤔 Why I Built This

All the existing tools (Moxfield, MPCFill.com, etc.) were **clunky and slow** – too many mouse clicks to select one image.  
I wanted a tool that lets you fly through a decklist with just the keyboard.  
(MPCFill has higher‑quality pictures, but it takes forever to finish a full deck.)

So I made MTG Art Picker: **fast navigation, minimal clicks, all in one place.**

---

# 🚀 DOWNLOAD (MOST USERS — NO PYTHON REQUIRED)

## 👉 IMPORTANT: GO TO **RELEASES** AND DOWNLOAD THE ZIP FILE

Click here:

👉 Releases page: [https://github.com/sk90y/mtg-art-picker/releases](https://github.com/sk90y/mtg-art-picker/releases)

👉 Direct download (v2.0.4):
`https://github.com/sk90y/mtg-art-picker/releases/download/v2.0.4/art.picker.2.0.4.zip`

Then:

1. Download **art.picker.2.0.4.zip**
2. Right-click → **Extract All**
3. Open the folder
4. Double-click **CLICK THIS TO RUN.bat**

✅ No installation required  
✅ Works on Windows 10 / 11  
✅ Python NOT needed  

If Windows shows **“Protected your PC”**:

- Click **More info**
- Click **Run anyway**

---

# ✨ What This Tool Does

This tool helps Magic players:

- Load decklists
- Browse all card printings from Scryfall
- Compare artwork visually
- Filter styles and versions
- Select preferred art
- Batch download/export card images

---

# 🧙 Who This Tool Is For

Magic: The Gathering players who want:

- High-quality proxy images
- Easy art comparison
- Fast bulk downloads
- A simple desktop app without coding

---

# 🖥 Screenshots

<img width="392" height="263" alt="image" src="https://github.com/user-attachments/assets/25164f8d-256a-4e42-a6e2-9c40a3c1ee2d" />
<img width="844" height="677" alt="image" src="https://github.com/user-attachments/assets/6129ee66-0433-4386-a15f-f0dbf89d8961" />
<img width="2298" height="1277" alt="image" src="https://github.com/user-attachments/assets/b9ceaaf4-0bde-46c2-9a4a-4f2e55ce682f" />
<img width="2298" height="1277" alt="Screenshot 2026-02-26 184915" src="https://github.com/user-attachments/assets/14e1989e-7b79-4e81-b803-5497cc6c0452" />
<img width="304" height="197" alt="Screenshot 2026-02-26 184945" src="https://github.com/user-attachments/assets/78227d1e-2f06-4c77-ba80-594ed978d1c5" />
<img width="1142" height="670" alt="Screenshot 2026-02-26 185118" src="https://github.com/user-attachments/assets/a4779367-c0fb-4c25-9e4a-461e42bc1e2b" />

---

## 📖 How to Use MTG Art Picker Efficiently

### 1. Starting a Project

- **New Project** – Choose a folder to store your project (selections and cache). Paste a decklist (one card per line, quantities like `2x Arcane Signet` are fine). You can also paste **token queries** in the token box (for example: `Human 1/1`, `treasure`, `cat 2/2`). The app converts shorthand and auto-adds `type:token` when needed.  
  **Important:** If you are copying from **Moxfield**, use **“Copy as Plain Text”** (or similar) to get just the card names. The tool is not designed to read Moxfield’s formatted output that includes set codes and collector numbers – the app will generate the print numbers based on **your** choices later.

  **Moxfield click path (recommended):**
  1. Click the **three dots** (`More`) in your deck page.
  2. Click **Export**.
  3. Click **Copy Plain Text**.

  ⚠️ Do **not** use export formats that include set code + collector number (for example, `Abyssal Gatekeeper (WTH) 59`). Those lines can fail exact-name lookup.
- **Continue Project** – Pick a recent project to resume where you left off.
- **Browse…** – Open any existing project folder.

### 2. Interface Overview

- **Left panel** – List of cards in your deck. ✅ marks selected cards.  
- **Top bar** – Filter controls to narrow down printings.  
- **Right panel** – Large preview of the current printing.  
- **Bottom scroll area** – Thumbnails of all printings (newest first).  

### 3. Filters Explained

Filters apply to **every card** in your deck (except when using **0** – see below). They are saved with your project.

- **Prefer borderless (fallback)** – When border is set to “Any”, this tries borderless first. If none exist, falls back to your other filters.  
- **Border** – `Any`, `Borderless`, `Black`, `White`, `Silver` (matches Scryfall’s `border:` syntax).  
- **Frame** – The card frame year: `1993`, `1997`, `2003`, `2015`, `Future`.  
- **Frame effect** – Special frames: `legendary`, `colorshifted`, `tombstone`, `enchantment`.  
- **Full art (is:full)** – Cards with extended or full‑bleed art.  
- **Hi-res (is:hires)** – Cards that Scryfall marks as high‑resolution (usually newer, sharper scans).  
- **Default (is:default)** – The “default” printing of a card (often the original or most common version).  
- **Atypical (is:atypical)** – Cards with unusual layouts (e.g., double‑faced, split cards).  
- **Exclude UB (not:universesbeyond)** – Hide Universes Beyond printings (like Lord of the Rings, Warhammer, etc.).  
- **Stamp** – The holofoil stamp type: `oval`, `acorn`, `triangle`, `arena`.  

**Tip:** Start with loose filters, then tighten as needed. If no printings appear, try clearing some filters.

**More Infromation about Scryfall’s syntax at: https://scryfall.com/docs/syntax**

### 4. Keyboard Shortcuts (Master These!)

| Key              | Action                                                                 |
|------------------|------------------------------------------------------------------------|
| **↑ / ↓**        | Move to previous / next card. **↓ also selects the current printing** (see below). |
| **← / →**        | Cycle through printings for the current card.                         |
| **0**            | **Toggle ALL PRINTS mode for THIS CARD only.** Ignores global filters and shows every printing of the card. Great for when you want to see everything, even if it doesn’t match your usual filters. |
| **U**            | Undo the last selection change (multiple steps).                       |
| **Backspace**    | Clear the selection for the current card.                              |
| **D**            | Download all selected images (you’ll be warned if some cards are unselected). |
| **G**            | Jump to a specific card number.                                        |
| **O**            | Open the current printing’s Scryfall page in your browser.            |
| **?**            | Show this help screen.                                                 |

### 5. Selecting Artwork

1. Use **← / →** or **click a thumbnail** to browse printings.  
2. When you see the art you want, press **↓** (Down arrow) – this **selects** that printing and automatically moves to the next card.  
   - The card in the left list will show a ✅ with the set and collector number.  
3. If you change your mind later, revisit the card, pick another printing, and press **↓** again – it overwrites the selection.  

The **blue border** highlights the currently viewed printing; **green border** marks the selected one.

### 6. Downloading Images

- Press **D** (or click the Download button).  
- If you haven’t selected every card, the app will ask if you want to download only the selected ones.  
- Choose an output folder. Images are saved as:  
  `Card Name [SET Collector#].png` (if PNG available) or `.jpg`.  
- You can cancel during download; already saved files remain.

All downloaded images are the **highest quality** available (PNG if Scryfall provides it, otherwise large JPG).

### 7. Project & Cache

Your project folder contains:

- `project.json` – deck list, current index, active printings, filters.  
- `selections.json` – your chosen printings for each card.  
- `cache/` – metadata and images saved locally. This speeds up future sessions and allows offline browsing of previously fetched cards.

You can close the app anytime and **Continue Project** later – everything is restored.



---

# 🛠 Run From Source (Mac / Linux / Developers)

If you’re on **Mac or Linux**, or you just want to run the Python script directly:

Requirements:

- Python 3.10+
- PySide6
- requests

```bash
pip install PySide6 requests
```

Run:

```bash
python mtg_art_picker.py
```

---

## ⚠ Disclaimer

This tool is a **hobbyist project** created for **personal, non-commercial use only**. It is intended to help Magic: The Gathering enthusiasts create proxies for **personal playtesting, cube design, or custom decks** – not for profit, mass production, or any commercial activity.

**You may NOT use this tool to:**

- Produce counterfeit cards for sale or trade
- Mass‑print cards for commercial purposes
- Distribute downloaded images in a way that infringes on Wizards of the Coast’s or artists’ rights

All card data and images are provided by the [Scryfall API](https://scryfall.com/docs/api). This project is **unofficial** and **not affiliated with Wizards of the Coast** or Scryfall in any way. The artwork belongs to its respective copyright holders. By using this tool, you agree to respect intellectual property rights and use the downloaded images solely for personal, non‑commercial purposes.

---

## 🤝 Contributing / Contact

I’m not committed to long‑term support, but I’ll tinker with this project as time allows.
If you’d like to help out, report a bug, or suggest a feature, feel free to:

Open a GitHub Issue

Pull requests are welcome – let’s make this tool even better together!
