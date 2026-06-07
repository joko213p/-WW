from __future__ import annotations

import os
import re
import asyncio
import logging
import shutil
from pathlib import Path
from functools import partial
from typing import List, Optional, Tuple

import instaloader
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
IG_USER     = os.environ.get("IG_USER", "")
IG_PASS     = os.environ.get("IG_PASS", "")
_raw_group  = os.environ.get("GROUP_ID", "")
GROUP_ID    = int(_raw_group) if _raw_group else None

DOWNLOAD_DIR   = Path("/tmp/ig_downloads")
MAX_FILE_BYTES = 50 * 1024 * 1024


# ── Helpers ───────────────────────────────────────────────────────────────────
def esc(text: str) -> str:
    """Echappe les caracteres speciaux HTML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_username(text: str) -> Optional[str]:
    text = text.strip()
    for pattern in (
        r"instagram\.com/([A-Za-z0-9._]+)/?",
        r"^@?([A-Za-z0-9._]{1,30})$",
    ):
        m = re.search(pattern, text)
        if m:
            username = m.group(1).rstrip("/")
            if username not in ("p", "reel", "stories", "explore", "accounts", "tv"):
                return username
    return None


def human_size(n: int) -> str:
    for unit in ("o", "Ko", "Mo"):
        if n < 1024:
            return "{:.0f} {}".format(n, unit)
        n /= 1024
    return "{:.1f} Go".format(n)


# ── Instaloader ───────────────────────────────────────────────────────────────
def _build_loader() -> instaloader.Instaloader:
    L = instaloader.Instaloader(
        dirname_pattern=str(DOWNLOAD_DIR / "{target}"),
        filename_pattern="{date_utc:%Y%m%d_%H%M%S}_{shortcode}",
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",
        quiet=True,
    )
    if IG_USER and IG_PASS:
        try:
            L.login(IG_USER, IG_PASS)
            logger.info("Connecte a Instagram en tant que %s", IG_USER)
        except Exception as exc:
            logger.warning("Connexion Instagram echouee : %s", exc)
    return L


def _sync_download(username: str, content_types: List[str]) -> Tuple[List[Path], List[str]]:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    profile_dir = DOWNLOAD_DIR / username
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    L = _build_loader()
    skipped: List[str] = []

    try:
        profile = instaloader.Profile.from_username(L.context, username)
    except instaloader.exceptions.ProfileNotExistsException:
        raise ValueError("Le profil @{} est introuvable ou prive.".format(username))
    except Exception as exc:
        raise ValueError("Impossible d'acceder au profil : {}".format(exc))

    if "posts" in content_types or "reels" in content_types:
        try:
            for post in profile.get_posts():
                try:
                    L.download_post(post, target=profile_dir)
                except Exception as exc:
                    skipped.append("post {}: {}".format(post.shortcode, exc))
        except Exception as exc:
            skipped.append("Lecture des posts impossible : {}".format(exc))

    if "stories" in content_types:
        if not (IG_USER and IG_PASS):
            skipped.append("Stories : identifiants IG_USER / IG_PASS requis.")
        else:
            try:
                L.download_stories(userids=[profile.userid], filename_target=profile_dir)
            except TypeError:
                try:
                    L.download_stories(userids=[profile.userid], fast_update=False)
                except Exception as exc:
                    skipped.append("Stories : {}".format(exc))
            except Exception as exc:
                skipped.append("Stories : {}".format(exc))

    if "highlights" in content_types:
        if not (IG_USER and IG_PASS):
            skipped.append("Stories a la une : identifiants IG_USER / IG_PASS requis.")
        else:
            try:
                for highlight in instaloader.Instaloader.get_highlights(L, profile):
                    try:
                        L.download_highlight(highlight, fast_update=False)
                    except Exception as exc:
                        skipped.append("Highlight {}: {}".format(highlight.title, exc))
            except Exception as exc:
                skipped.append("Highlights : {}".format(exc))

    files: List[Path] = []
    if profile_dir.exists():
        for ext in ("*.mp4", "*.jpg", "*.jpeg", "*.png", "*.webp"):
            for f in profile_dir.rglob(ext):
                size = f.stat().st_size
                if size <= MAX_FILE_BYTES:
                    files.append(f)
                else:
                    skipped.append("{} ({} > 50 Mo)".format(f.name, human_size(size)))

    files.sort()
    return files, skipped


# ── Topic supergroupe ─────────────────────────────────────────────────────────
async def get_or_create_topic(bot, chat_id: int, username: str) -> Optional[int]:
    try:
        forum_topic = await bot.create_forum_topic(
            chat_id=chat_id,
            name="@{}".format(username),
        )
        return forum_topic.message_thread_id
    except TelegramError as exc:
        err = str(exc).lower()
        if any(kw in err for kw in ("not a supergroup", "forum", "not supported", "chat not found")):
            return None
        logger.warning("create_forum_topic : %s", exc)
        return None


# ── Envoi fichier ─────────────────────────────────────────────────────────────
async def send_file(bot, chat_id: int, thread_id: Optional[int], f: Path, caption: str):
    kwargs = dict(chat_id=chat_id, caption=caption)
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    with open(f, "rb") as fh:
        if f.suffix.lower() == ".mp4":
            await bot.send_video(video=fh, **kwargs)
        else:
            await bot.send_photo(photo=fh, **kwargs)


# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Bienvenue sur Instagram Downloader Bot !</b>\n\n"
        "Envoie-moi un lien de profil Instagram ou un <b>@username</b> "
        "et je telechargerai les medias de ton choix.\n\n"
        "Exemples :\n"
        "• <code>https://www.instagram.com/natgeo</code>\n"
        "• <code>@natgeo</code>\n\n"
        "💡 <b>Supergroupe avec Topics</b> : chaque profil sera automatiquement "
        "classe dans son propre fil.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    has_creds = bool(IG_USER and IG_PASS)
    creds_status = "✅ configures" if has_creds else "❌ non configures (IG_USER / IG_PASS manquants)"
    await update.message.reply_text(
        "📖 <b>Aide</b>\n\n"
        "<b>Comment utiliser :</b>\n"
        "1. Envoie un lien Instagram ou un @username\n"
        "2. Choisis le type de contenu\n"
        "3. Recois les fichiers (max 50 Mo/fichier)\n\n"
        "<b>Types disponibles :</b>\n"
        "📸 Posts  |  🎬 Reels  |  📖 Stories  |  ⭐ A la une\n\n"
        "<b>Identifiants Instagram :</b> {}\n"
        "<i>Stories &amp; A la une necessitent un compte Instagram connecte</i>".format(creds_status),
        parse_mode=ParseMode.HTML,
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    username = extract_username(text)

    if not username:
        await update.message.reply_text(
            "❌ Profil Instagram non reconnu.\n"
            "Envoie un lien <code>https://www.instagram.com/username</code> ou <code>@username</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    context.user_data["ig_username"] = username

    has_creds = bool(IG_USER and IG_PASS)
    note = (
        ""
        if has_creds
        else "\n\n⚠️ <i>Stories &amp; A la une : IG_USER / IG_PASS non configures.</i>"
    )

    keyboard = [
        [
            InlineKeyboardButton("📸 Posts",    callback_data="type_posts"),
            InlineKeyboardButton("🎬 Reels",    callback_data="type_reels"),
        ],
        [
            InlineKeyboardButton("📖 Stories",  callback_data="type_stories"),
            InlineKeyboardButton("⭐ A la une", callback_data="type_highlights"),
        ],
        [InlineKeyboardButton("✅ Tout telecharger", callback_data="type_all")],
    ]

    await update.message.reply_text(
        "📲 Profil detecte : <b>@{}</b>\n\nQue veux-tu telecharger ?{}".format(esc(username), note),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_type_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    username = context.user_data.get("ig_username")
    if not username:
        await query.edit_message_text("❌ Session expiree. Renvoie le lien Instagram.")
        return

    type_map = {
        "type_posts":      ["posts"],
        "type_reels":      ["reels"],
        "type_stories":    ["stories"],
        "type_highlights": ["highlights"],
        "type_all":        ["posts", "reels", "stories", "highlights"],
    }
    content_types = type_map.get(query.data, ["posts"])
    chat_id = GROUP_ID if GROUP_ID else update.effective_chat.id
    bot = context.bot

    await query.edit_message_text(
        "⏳ Telechargement de <b>@{}</b> en cours…".format(esc(username)),
        parse_mode=ParseMode.HTML,
    )

    loop = asyncio.get_event_loop()
    try:
        files, skipped = await loop.run_in_executor(
            None,
            partial(_sync_download, username, content_types),
        )
    except ValueError as exc:
        await query.edit_message_text("❌ {}".format(esc(str(exc))), parse_mode=ParseMode.HTML)
        return
    except Exception as exc:
        logger.exception("Erreur de telechargement inattendue")
        await query.edit_message_text("❌ Erreur inattendue : {}".format(esc(str(exc))), parse_mode=ParseMode.HTML)
        return

    if not files:
        reasons = (
            "\n".join("• {}".format(esc(s)) for s in skipped)
            if skipped
            else "Aucun media trouve."
        )
        await query.edit_message_text(
            "😕 Aucun fichier pour <b>@{}</b>.\n\n{}".format(esc(username), reasons),
            parse_mode=ParseMode.HTML,
        )
        return

    thread_id = await get_or_create_topic(bot, chat_id, username)

    await query.edit_message_text(
        "📤 Envoi de <b>{}</b> fichier(s) pour <b>@{}</b>…".format(len(files), esc(username)),
        parse_mode=ParseMode.HTML,
    )

    sent = 0
    for f in files:
        try:
            await send_file(bot, chat_id, thread_id, f, caption="@{}".format(username))
            sent += 1
            await asyncio.sleep(0.4)
        except TelegramError as exc:
            skipped.append("{} : {}".format(f.name, exc))

    summary = "✅ <b>{}/{}</b> fichier(s) envoye(s) pour <b>@{}</b>.".format(sent, len(files), esc(username))
    if skipped:
        items = "\n".join("• {}".format(esc(s)) for s in skipped[:10])
        summary += "\n\n⚠️ <b>Ignores :</b>\n{}".format(items)

    await query.edit_message_text(summary, parse_mode=ParseMode.HTML)

    profile_dir = DOWNLOAD_DIR / username
    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CallbackQueryHandler(handle_type_choice, pattern=r"^type_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    logger.info("Bot Instagram Downloader demarre.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
