import os
import discord
import anthropic
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

# ---- クライアント初期化 ----
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

ai = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = (
    "You are a helpful, friendly assistant on a Discord server. "
    "Be concise and format your responses for Discord (use markdown where helpful). "
    "If asked in Japanese, respond in Japanese."
)

MAX_HISTORY = 20  # チャンネルごとに保持するメッセージ数
conversation_history: dict[str, list[dict]] = defaultdict(list)


# ---- ヘルパー ----
async def send_long(channel: discord.abc.Messageable, text: str, reference=None):
    """2000文字のDiscord制限に合わせてメッセージを分割して送信する。"""
    chunks = [text[i : i + 1990] for i in range(0, len(text), 1990)]
    for i, chunk in enumerate(chunks):
        if i == 0 and reference:
            await reference.reply(chunk)
        else:
            await channel.send(chunk)


async def ask_claude(channel_id: str, user_text: str) -> str:
    """会話履歴を使ってClaudeを呼び出し、回答を返す。"""
    history = conversation_history[channel_id]
    history.append({"role": "user", "content": user_text})

    # システムプロンプトをキャッシュして費用を削減
    with ai.messages.stream(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=history[-MAX_HISTORY:],
    ) as stream:
        reply = stream.get_final_message().content[0].text

    history.append({"role": "assistant", "content": reply})

    # 古い履歴を削除
    if len(history) > MAX_HISTORY * 2:
        conversation_history[channel_id] = history[-(MAX_HISTORY * 2) :]

    return reply


# ---- イベントハンドラ ----
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    # ボット自身のメッセージは無視
    if message.author.bot:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions

    # DMまたはメンションのときだけ反応
    if not (is_dm or is_mentioned):
        return

    # メンションを除いたテキストを取得
    content = message.content
    if is_mentioned:
        content = content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()

    # !reset コマンド
    if content.strip().lower() in ("!reset", "/reset"):
        conversation_history[str(message.channel.id)].clear()
        await message.reply("🔄 会話履歴をリセットしました。")
        return

    if not content:
        await message.reply("何かメッセージを入力してください。")
        return

    async with message.channel.typing():
        try:
            reply = await ask_claude(str(message.channel.id), content)
            await send_long(message.channel, reply, reference=message)
        except anthropic.APIStatusError as e:
            await message.reply(f"⚠️ API エラー ({e.status_code}): {e.message}")
        except Exception as e:
            await message.reply(f"⚠️ エラーが発生しました: {e}")


bot.run(os.environ["DISCORD_BOT_TOKEN"])
