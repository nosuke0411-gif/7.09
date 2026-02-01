import discord
from discord.ext import commands
from discord import app_commands
import random
import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json
import os
from threading import Thread
from flask import Flask
from typing import Literal, Optional
from discord import app_commands
from flask import request

# --- Discord Bot 設定 ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- FlaskでRender用ダミーサーバー ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

@app.route('/reset_spent', methods=['POST'])
def reset_spent():
    token = request.args.get("token")
    if token != os.getenv("RESET_SECRET"):
        return "Unauthorized", 403

    reset_all_spent_coins()

    # 通知を非同期で送信
    bot.loop.create_task(send_reset_notification())

    return "✅ リセット完了！", 200
def run():

    app.run(host='0.0.0.0', port=8080)

Thread(target=run).start()

# --- Google Sheets 接続 ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS"))
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open("nosuke_data").sheet1
explore_level_sheet = client.open("nosuke_data").worksheet("explore_levels")
explore_status_sheet = client.open("nosuke_data").worksheet("explore_status")
explore_items_sheet = client.open("nosuke_data").worksheet("explore_items")
dice_streaks_sheet = client.open("nosuke_data").worksheet("dice_streaks")
spent_coins_sheet = client.open("nosuke_data").worksheet("spent_coins")

# --- 定数・初期データ ---
STARTING_COINS = 100
SLOTS = ["🍒", "🍋", "🍊", "🍇", "⭐"]
DAILY_BONUSES = {0: 100, 1: 150, 2: 200, 3: 250, 4: 300, 5: 400, 6: 700}
RANK_BONUSES = [1000, 700, 500, 300, 200]

SPECIAL_ITEMS = [
    "木の実のかご", "光る石", "ぬれたお守り", "古びたコイン", "狐の面",
    "特製おやつ", "祈りの結晶", "金のコイン", "神秘の面"
]

ITEM_DESCRIPTIONS = {
    "木の実のかご": {"emoji": "🍒", "description": "探検中に使うと、残り時間を15分短縮できるよ！"},
    "光る石": {"emoji": "💎", "description": "使うと、次の探検で獲得EXPが1.5倍になるよ！"},
    "ぬれたお守り": {"emoji": "💧", "description": "使うと、次の探検でスーパーチャームの発見率が+20%になるよ！"},
    "古びたコイン": {"emoji": "🪙", "description": "使うと、即100コインが手に入るよ！"},
    "狐の面": {"emoji": "🦊", "description": "使うと、スーパーチャームが1個手に入るよ！"},
    "特製おやつ": {
        "emoji": "🧺",
        "description": "探検中に使うと、残り時間を60分短縮できるよ！",
        "recipe": ["木の実のかご ×3"]
    },
    "金のコイン": {
        "emoji": "🪙✨",
        "description": "使うと500コインが手に入るよ！",
        "recipe": ["古びたコイン ×3"]
    },
    "祈りの結晶": {
        "emoji": "🔮",
        "description": "次の探検でEXP +50%、発見率 +20%！幻のエリアが出るかも…？",
        "recipe": ["光る石 ×1", "ぬれたお守り ×1"]
    },
    "神秘の面": {
        "emoji": "🦊🔥",
        "description": "スーパーチャーム +2、次の探検でEXPアップ！幻のキツネが出るかも？",
        "recipe": ["狐の面 ×2", "祈りの結晶 ×1"]
    },
}

# --- JSONファイル読み書き ---
def load_json(filename):
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return json.load(f)
    return {}

def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f)

# --- ファイル読み込み ---
COIN_FILE = "user_coins.json"
CHARM_FILE = "charms.json"
SUPER_CHARM_FILE = "super_charms.json"
SUPER_CHARM_ACTIVE_FILE = "super_charm_active.json"
BANK_FILE = "bank.json"
LAST_INTEREST_WEEK_FILE = "last_interest_week.json"
DAILY_FILE = "daily_bonus.json"
RANK_FILE = "rank_bonus.json"
EXPLORE_FILE = "explore_status.json"
USED_ITEM_FILE = "used_items.json"
DISCOUNT_FILE = "daily_discount.json"
daily_discount = load_json(DISCOUNT_FILE)

user_coins = load_json(COIN_FILE)
user_charms = load_json(CHARM_FILE)
user_super_charms = load_json(SUPER_CHARM_FILE)
user_super_charm_active = load_json(SUPER_CHARM_ACTIVE_FILE)
user_bank = load_json(BANK_FILE)
last_interest_week = load_json(LAST_INTEREST_WEEK_FILE)
daily_claims = load_json(DAILY_FILE)
rank_claims = load_json(RANK_FILE)
explore_status = load_json(EXPLORE_FILE)
used_items = load_json(USED_ITEM_FILE)
# --- 銀行管理 ---
def get_bank(user_id):
    _, row = get_user_row(user_id)
    return int(row["bank"]) if row else 0

def set_bank(user_id, amount):
    row_num, row = get_user_row(user_id)
    if row:
        sheet.update_cell(row_num, 5, amount)
    else:
        ensure_user_exists(user_id)
        set_bank(user_id, amount)

# --- 利息管理 ---
def apply_weekly_interest():
    now = datetime.datetime.utcnow()
    current_week = f"{now.year}-W{now.isocalendar().week:02d}"
    records = sheet.get_all_records()

    for i, row in enumerate(records):
        user_id = str(row["user_id"])
        last_week = row.get("last_interest", "")
        bank = int(row.get("bank", 0))

        if bank > 0 and last_week != current_week:
            interest = max(1, int(bank * 0.01))
            new_bank = bank + interest
            sheet.update_cell(i + 2, 5, new_bank)
            sheet.update_cell(i + 2, 6, current_week)

# --- カード管理 ---
suits = ['♠', '♥', '♦', '♣']
ranks = ['A'] + [str(n) for n in range(2, 11)] + ['J', 'Q', 'K']

def draw_card():
    return random.choice(ranks) + random.choice(suits)

def card_value(card):
    rank = card[:-1]
    if rank in ['J', 'Q', 'K']:
        return 10
    elif rank == 'A':
        return 11
    else:
        return int(rank)

def hand_value(hand):
    total = sum(card_value(c) for c in hand)
    aces = sum(1 for c in hand if c.startswith('A'))
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total

#---ハイロー用カード---
def card_value_highlow(card):
    rank = card[:-1]
    if rank == 'J':
        return 11
    elif rank == 'Q':
        return 12
    elif rank == 'K':
        return 13
    elif rank == 'A':
        return 1   # Aは1にする（13より下）
    else:
        return int(rank)
# --- プレイヤー状態管理 ---
sessions = {}

class BlackjackView(discord.ui.View):
    def __init__(self, user_id, bet):
        super().__init__(timeout=60)
        self.user_id = str(user_id)  # ← 統一
        self.bet = bet

    def disable_double_if_needed(self):
        session = sessions.get(self.user_id)
        if session and session.get("has_hit"):
            self.double.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.user_id  # ← 統一

    @discord.ui.button(label="ヒット", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = sessions.get(self.user_id)
        session["player"].append(draw_card())
        session["has_hit"] = True

        value = hand_value(session["player"])
        if value > 21:
            await self.end_game(interaction, result="bust")
        else:
            await self.update_message(interaction)

    @discord.ui.button(label="スタンド", style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = sessions.get(self.user_id)
        while hand_value(session["dealer"]) < 17:
            session["dealer"].append(draw_card())
        await self.end_game(interaction, result="stand")

    @discord.ui.button(label="ダブル", style=discord.ButtonStyle.success, row=1)
    async def double(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = sessions.get(self.user_id)
        user_id = self.user_id  # ← ここも str で統一
        coins = get_coins(user_id)

        if session["has_hit"]:
            await interaction.response.send_message("⚠️ ダブルは最初の手番でしか使えないよ！", ephemeral=True)
            return

        if coins < self.bet:
            await interaction.response.send_message("💸 コインが足りなくてダブルできないよ！", ephemeral=True)
            return

        # 正しい順番
        self.bet *= 2
        set_coins(user_id, coins - self.bet)
        add_spent_coins(user_id, self.bet)

        session["player"].append(draw_card())
        await self.end_game(interaction, result="stand")

    async def update_message(self, interaction):
        session = sessions.get(self.user_id)
        player_hand = session["player"]
        dealer_hand = [session["dealer"][0], "❓"]

        self.disable_double_if_needed()

        msg = (
            f"🃏 **ブラックジャック**\n\n"
            f"**あなた** | {hand_value(player_hand)}\n{' '.join(player_hand)}\n"
            f"**ディーラー** | ?\n{' '.join(dealer_hand)}\n\n"
            f"👉 ヒット or スタンド？"
        )
        await interaction.response.edit_message(content=msg, view=self)

    async def end_game(self, interaction, result):
        session = sessions.get(self.user_id)
        player = session["player"]
        dealer = session["dealer"]
        bet = self.bet
        player_val = hand_value(player)
        dealer_val = hand_value(dealer)

        if result == "bust":
            outcome = "💥 バースト！あなたの負け！"
            delta = 0
        else:
            if player_val > 21:
                outcome = "💥 バースト！あなたの負け！"
                delta = 0
            elif dealer_val > 21 or player_val > dealer_val:
                outcome = "🎉 勝利！"
                delta = bet * 2
            elif player_val == dealer_val:
                outcome = "🤝 引き分け！"
                delta = bet
            else:
                outcome = "😢 負けちゃった！"
                delta = 0

        user_id = self.user_id  # ← 統一
        coins = get_coins(user_id)
        coins += delta
        set_coins(user_id, coins)

        # 使用額を加算（引き分けは除く）
        if delta != 0:
            add_spent_coins(user_id, abs(delta))

        msg = (
            f"🃏 **ブラックジャック 結果発表！**\n\n"
            f"**あなた** | {player_val}\n{' '.join(player)}\n"
            f"**ディーラー** | {dealer_val}\n{' '.join(dealer)}\n\n"
            f"{outcome}\n"
            f"{'🪙 勝って ' + str(bet * 2) + ' コイン獲得！' if delta > 0 else '🪙 ' + str(abs(delta)) + ' コイン失ったよ…' if delta < 0 else '🪙 コインは戻ってきたよ！'}\n"
            f"💰 現在のコイン残高: {coins}"
        )

        await interaction.response.edit_message(content=msg, view=None)
        sessions.pop(self.user_id, None)
#---ショップ管理---
SHOP_ITEMS = {
    "木の実のかご": 200,
    "光る石": 500,
    "ぬれたお守り": 500,
    "古びたコイン": 150,
    "狐の面": 1000
}

#---ショップボタン管理---
class ShopView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = str(user_id)  # ← 統一

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.user_id  # ← 統一

    @discord.ui.button(label="木の実のかご (200)", style=discord.ButtonStyle.primary)
    async def buy_kago(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.buy_item(interaction, "木の実のかご")

    @discord.ui.button(label="光る石 (500)", style=discord.ButtonStyle.primary)
    async def buy_hikarui(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.buy_item(interaction, "光る石")

    @discord.ui.button(label="ぬれたお守り (500)", style=discord.ButtonStyle.primary)
    async def buy_omamori(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.buy_item(interaction, "ぬれたお守り")

    @discord.ui.button(label="古びたコイン (150)", style=discord.ButtonStyle.primary)
    async def buy_furui(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.buy_item(interaction, "古びたコイン")

    @discord.ui.button(label="狐の面 (1000)", style=discord.ButtonStyle.success)
    async def buy_kitsune(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.buy_item(interaction, "狐の面")

    async def buy_item(self, interaction, item_name):
        discount_item = choose_daily_discount()
        price = SHOP_ITEMS[item_name]
        if item_name == discount_item:
            price = price // 2

        coins = get_coins(self.user_id)
        if coins < price:
            await interaction.response.send_message("💸 コインが足りないよ！", ephemeral=True)
            return

        set_coins(self.user_id, coins - price)
        add_user_item(self.user_id, item_name, 1)

        emoji = ITEM_DESCRIPTIONS[item_name]["emoji"]
        await interaction.response.send_message(
            f"{emoji} **{item_name}** を {price} コインで購入したよ！",
            ephemeral=True
        )

#---ショップ割引---
def choose_daily_discount():
    today = datetime.datetime.utcnow().date().isoformat()

    # すでに今日の割引が決まっているなら再利用
    if daily_discount.get("date") == today:
        return daily_discount["item"]

    # 新しく選ぶ
    item = random.choice(list(SHOP_ITEMS.keys()))
    daily_discount["date"] = today
    daily_discount["item"] = item
    save_json(DISCOUNT_FILE, daily_discount)

    return item
#---ハイロー管理---
class HighLowView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = str(user_id)  # ← 統一

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.user_id  # ← 統一

    @discord.ui.button(label="High", style=discord.ButtonStyle.success)
    async def high(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve(interaction, "high")

    @discord.ui.button(label="Low", style=discord.ButtonStyle.primary)
    async def low(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve(interaction, "low")

    async def resolve(self, interaction, guess):
        session = sessions.get(self.user_id)
        bet = session["bet"]
        first = session["current_card"]
        first_val = card_value_highlow(first)

        second = draw_card()
        second_val = card_value_highlow(second)

        odds = get_highlow_odds(first_val)

        # 判定
        if second_val == first_val:
            result = "🤝 引き分け！"
            delta = 0
            next_view = HighLowContinueView(self.user_id)

        elif (second_val > first_val and guess == "high") or (second_val < first_val and guess == "low"):
            result = "🎉 勝利！"
            session["multiplier"] *= odds[guess]
            delta = 0
            next_view = HighLowContinueView(self.user_id)

        else:
            result = "😢 負けちゃった！"
            delta = 0
            next_view = None

        # コイン更新
        coins = get_coins(self.user_id) + delta
        set_coins(self.user_id, coins)

        msg = (
            f"🎲 **ハイロー結果！**\n\n"
            f"前のカード：{first}（{first_val}）\n"
            f"次のカード：{second}（{second_val}）\n\n"
            f"{result}\n"
            f"現在の倍率：x{session['multiplier']:.3f}\n"
            f"💰 現在のコイン残高: {coins}"
        )

        if next_view is None:
            sessions.pop(self.user_id, None)
            await interaction.response.edit_message(content=msg, view=None)
            return

        session["current_card"] = second
        msg += "\n\n続ける？ストップ？"
        await interaction.response.edit_message(content=msg, view=next_view)


#---ストップ管理---
class HighLowContinueView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = str(user_id)  # ← 統一

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.user_id  # ← 統一

    @discord.ui.button(label="続ける", style=discord.ButtonStyle.success)
    async def cont(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = sessions.get(self.user_id)
        card = session["current_card"]
        val = card_value_highlow(card)
        odds = get_highlow_odds(val)

        msg = (
            f"🎲 **ハイロー続行！**\n\n"
            f"現在のカード：{card}（{val}）\n"
            f"High 倍率：x{odds['high']}\n"
            f"Low 倍率：x{odds['low']}\n\n"
            "High or Low？"
        )

        await interaction.response.edit_message(content=msg, view=HighLowView(self.user_id))

    @discord.ui.button(label="ストップ", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = sessions.get(self.user_id)
        payout = int(session["bet"] * session["multiplier"])

        coins = get_coins(self.user_id) + payout
        set_coins(self.user_id, coins)

        sessions.pop(self.user_id, None)

        msg = (
            f"🎮 ハイロー終了！\n"
            f"最終倍率：x{session['multiplier']:.3f}\n"
            f"🪙 {payout} コイン獲得！\n"
            f"💰 現在のコイン残高: {coins}"
        )

        await interaction.response.edit_message(content=msg, view=None)


#---倍率テーブル---
def get_highlow_odds(value):
    high_prob = (13 - value) / 13
    high_odds = 1 / high_prob if high_prob > 0 else 0

    low_prob = (value - 1) / 13
    low_odds = 1 / low_prob if low_prob > 0 else 0

    return {
        "high": round(high_odds, 3),
        "low": round(low_odds, 3)
    }
# ■ マイン倍率計算（本家方式）
def calculate_mines_multiplier(opened, bombs):
    total = 20
    safe = total - bombs

    multiplier = 1.0
    for i in range(opened):
        multiplier *= (safe - i) / (total - i)

    return round(multiplier * 1.15, 3)

# ■ 盤面生成（4×5）
def render_mines_board(session, reveal=False):
    board = []
    for i in range(20):
        if reveal:
            if i in session["bomb_positions"]:
                board.append("💣")
            elif i in session["opened"]:
                board.append("🟩")
            else:
                board.append("⬜")
        else:
            if i in session["opened"]:
                board.append("🟩")
            else:
                board.append("⬜")

    rows = ["".join(board[i:i+5]) for i in range(0, 20, 5)]
    return "\n".join(rows)

# ■ Mines 全体 View（20マス＋ストップ＝1View）
class MinesView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=120)
        self.user_id = str(user_id)  # ← 統一

        # 20マス（4×5）
        for i in range(20):
            self.add_item(MinesButton(i, self.user_id))  # ← 統一

        # ストップボタン（row=4）
        self.add_item(MinesStopButton(self.user_id))  # ← 統一

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.user_id  # ← 統一


# ■ マスボタン（20個）
class MinesButton(discord.ui.Button):
    def __init__(self, index, user_id):
        super().__init__(label=" ", style=discord.ButtonStyle.secondary, row=index // 5)
        self.index = index
        self.user_id = str(user_id)  # ← 統一

    async def callback(self, interaction: discord.Interaction):
        session = sessions.get(self.user_id)
        if not session:
            await interaction.response.send_message("⚠️ セッションが見つからないよ！", ephemeral=True)
            return

        if self.index in session["opened"]:
            await interaction.response.send_message("⚠️ そのマスはもう開いてるよ！", ephemeral=True)
            return

        # 爆弾
        if self.index in session["bomb_positions"]:
            board = render_mines_board(session, reveal=True)
            bet = session["bet"]
            sessions.pop(self.user_id, None)

            await interaction.response.edit_message(
                content=f"💥 **BOOM!! 爆弾を踏んじゃった…**\n\n🪙 {bet} コイン失ったよ…\n\n{board}",
                view=None
            )
            return

        # セーフ
        session["opened"].add(self.index)
        opened_count = len(session["opened"])
        session["multiplier"] = calculate_mines_multiplier(opened_count, session["bombs"])

        board = render_mines_board(session)

        # View を再生成（常に1View）
        new_view = MinesView(self.user_id)

        await interaction.response.edit_message(
            content=(
                f"✨ **セーフ！**\n"
                f"現在倍率：x{session['multiplier']}\n\n"
                f"{board}\n\n"
                "続ける？ストップ？"
            ),
            view=new_view
        )


# ■ ストップボタン
class MinesStopButton(discord.ui.Button):
    def __init__(self, user_id):
        super().__init__(label="ストップ", style=discord.ButtonStyle.danger, row=4)
        self.user_id = str(user_id)  # ← 統一

    async def callback(self, interaction: discord.Interaction):
        session = sessions.get(self.user_id)
        if not session:
            await interaction.response.send_message("⚠️ セッションが見つからないよ！", ephemeral=True)
            return

        bet = session["bet"]
        payout = int(bet * session["multiplier"])
        coins = get_coins(self.user_id) + payout
        set_coins(self.user_id, coins)

        board = render_mines_board(session, reveal=True)
        sessions.pop(self.user_id, None)

        await interaction.response.edit_message(
            content=(
                f"🎉 **キャッシュアウト！**\n"
                f"倍率：x{session['multiplier']}\n"
                f"🪙 {payout} コイン獲得！\n\n"
                f"{board}\n"
                f"💰 現在のコイン残高：{coins}"
            ),
            view=None
        )
# --- スーパーチャーム管理 ---
def get_super_charm_count(user_id):
    _, row = get_user_row(user_id)
    return int(row["super_charm"]) if row else 0

def set_super_charm_count(user_id, count):
    row_num, row = get_user_row(user_id)
    if row:
        sheet.update_cell(row_num, 7, count)
    else:
        ensure_user_exists(user_id)
        set_super_charm_count(user_id, count)

#---ランク管理---
def get_spent_coins(user_id):
    records = spent_coins_sheet.get_all_records()
    for row in records:
        if str(row["user_id"]) == str(user_id):
            return int(row["spent"])
    return 0

def add_spent_coins(user_id, amount):
    records = spent_coins_sheet.get_all_records()
    for i, row in enumerate(records):
        if str(row["user_id"]) == str(user_id):
            new_total = int(row["spent"]) + amount
            spent_coins_sheet.update_cell(i + 2, 2, new_total)
            return
    # 新規ユーザー
    spent_coins_sheet.append_row([str(user_id), amount])

def get_user_rank(user_id):
    spent = get_spent_coins(user_id)
    if spent >= 300_000:
        return "👑 VIP"
    elif spent >= 200_000:
        return "💼 プロ"
    elif spent >= 150_000:
        return "🎯 マスター"
    elif spent >= 100_000:
        return "🔥 レジェンド"
    elif spent >= 75_000:
        return "💎 エリート"
    elif spent >= 50_000:
        return "🔷 ダイヤ"
    elif spent >= 30_000:
        return "🥇 ゴールド"
    elif spent >= 15_000:
        return "🥈 シルバー"
    elif spent >= 5_000:
        return "🥉 ブロンズ"
    else:
        return "🔰 ビギナー"

VIP_ROLE_NAME = "VIP"

#---ロール付け外し---
async def update_vip_role(member: discord.Member, is_vip: bool):
    if not is_vip:
        return  # VIPじゃないなら何もしない（ロールは外さない）

    guild = member.guild
    vip_role = discord.utils.get(guild.roles, name=VIP_ROLE_NAME)
    if not vip_role:
        return

    if vip_role not in member.roles:
        await member.add_roles(vip_role)
#---リセット通知---
async def send_reset_notification():
    await bot.wait_until_ready()
    channel = bot.get_channel(RESET_NOTIFY_CHANNEL_ID)
    if channel:
        now = datetime.datetime.now().strftime("%Y/%m/%d %H:%M")
        await channel.send(f"🔁 コイン使用量のリセットを実行しました！（{now}）")

#---一斉リセット---
RESET_NOTIFY_CHANNEL_ID = int(os.getenv("RESET_NOTIFY_CHANNEL_ID"))

def reset_all_spent_coins():
    now = datetime.datetime.utcnow()
    current_period = f"{now.year}-{now.month:02d}"

    records = spent_coins_sheet.get_all_records()
    for i, row in enumerate(records):
        spent_coins_sheet.update_cell(i + 2, 2, 0)  # spent を 0 に
        spent_coins_sheet.update_cell(i + 2, 3, current_period)  # last_reset を更新
# --- アイテム管理 ---
def get_user_items(user_id):
    records = explore_items_sheet.get_all_records()
    for row in records:
        if str(row["user_id"]) == str(user_id):
            return {item: int(row.get(item, 0)) for item in SPECIAL_ITEMS}
    return {item: 0 for item in SPECIAL_ITEMS}

def add_user_item(user_id, item_name, amount=1):
    records = explore_items_sheet.get_all_records()
    for i, row in enumerate(records):
        if str(row["user_id"]) == str(user_id):
            current = int(row.get(item_name, 0))
            col_index = SPECIAL_ITEMS.index(item_name) + 2
            explore_items_sheet.update_cell(i + 2, col_index, current + amount)
            return
    new_row = [str(user_id)] + [0] * len(SPECIAL_ITEMS)
    new_row[SPECIAL_ITEMS.index(item_name) + 1] = amount
    explore_items_sheet.append_row(new_row)

def consume_user_item(user_id, item_name, amount=1):
    items = get_user_items(user_id)
    if items[item_name] >= amount:
        add_user_item(user_id, item_name, -amount)
        return True
    return False
# --- 探検ロケーションと設定 ---
EXPLORE_LOCATIONS = {
    "森": {"emoji": "🌲", "bonus": 1.0, "drop": "木の実のかご"},
    "山": {"emoji": "⛰️", "bonus": 1.1, "drop": "光る石"},
    "川辺": {"emoji": "🏞️", "bonus": 1.2, "drop": "ぬれたお守り"},
    "廃墟": {"emoji": "🏚️", "bonus": 1.3, "drop": "古びたコイン"},
    "神社": {"emoji": "⛩️", "bonus": 1.5, "drop": "狐の面"}
}

EXPLORE_OPTIONS = {
    5: {"min": 0, "max": 30, "charm": False},
    15: {"min": 10, "max": 80, "charm": False},
    30: {"min": 30, "max": 150, "charm": False},
    60: {"min": 50, "max": 200, "charm": True},
    120: {"min": 100, "max": 300, "charm": True},
    180: {"min": 150, "max": 400, "charm": True},
    360: {"min": 300, "max": 600, "charm": True}
}

# --- 探検状態の読み書き ---
def get_explore_status(user_id):
    records = explore_status_sheet.get_all_records()
    for row in records:
        if str(row["user_id"]) == str(user_id):
            return row["end_time"], int(row["minutes"]), row.get("location", "森")
    return None

def set_explore_status(user_id, end_time, minutes, location):
    records = explore_status_sheet.get_all_records()
    for i, row in enumerate(records):
        if str(row["user_id"]) == str(user_id):
            explore_status_sheet.update_cell(i + 2, 2, end_time)
            explore_status_sheet.update_cell(i + 2, 3, minutes)
            explore_status_sheet.update_cell(i + 2, 4, location)
            return
    explore_status_sheet.append_row([str(user_id), end_time, minutes, location])

def clear_explore_status(user_id):
    records = explore_status_sheet.get_all_records()
    for i, row in enumerate(records):
        if str(row["user_id"]) == str(user_id):
            explore_status_sheet.delete_rows(i + 2)
            return
def is_super_charm_active(user_id):
    return user_super_charm_active.get(user_id, False)

def set_super_charm_active(user_id, active):
    user_super_charm_active[user_id] = active
    save_json(SUPER_CHARM_ACTIVE_FILE, user_super_charm_active)

# --- 探検レベルと経験値 ---
def get_required_exp(level: int) -> int:
    table = {1: 50, 2: 100, 3: 150, 4: 200, 5: 300, 6: 400, 7: 500, 8: 600, 9: 800}
    return table.get(level, 9999)

def get_explore_data(user_id):
    records = explore_level_sheet.get_all_records()
    for row in records:
        if str(row["user_id"]) == str(user_id):
            return int(row["level"]), int(row["exp"])
    return 1, 0

def set_explore_data(user_id, level, exp):
    records = explore_level_sheet.get_all_records()
    for i, row in enumerate(records):
        if str(row["user_id"]) == str(user_id):
            explore_level_sheet.update_cell(i + 2, 2, level)
            explore_level_sheet.update_cell(i + 2, 3, exp)
            return
    explore_level_sheet.append_row([str(user_id), level, exp])

def add_explore_exp(user_id: str, gained_exp: int):
    level, exp = get_explore_data(user_id)
    exp += gained_exp
    while exp >= get_required_exp(level) and level < 10:
        exp -= get_required_exp(level)
        level += 1
    set_explore_data(user_id, level, exp)
    return level

def get_explore_bonus(level: int):
    bonus = 1.0
    charm_bonus = 0.0
    if level >= 2: bonus += 0.05
    if level >= 4: bonus += 0.05
    if level >= 7: bonus += 0.05
    if level >= 3: charm_bonus += 0.02
    if level >= 6: charm_bonus += 0.05
    if level >= 9: charm_bonus += 0.10
    return bonus, charm_bonus
#連勝データの取得
def get_dice_streak(user_id):
    records = dice_streaks_sheet.get_all_records()
    for row in records:
        if str(row["user_id"]) == str(user_id):
            return int(row.get("current", 0)), int(row.get("max", 0))
    return 0, 0

#連勝データの更新
def set_dice_streak(user_id, current, max_streak):
    records = dice_streaks_sheet.get_all_records()
    for i, row in enumerate(records):
        if str(row["user_id"]) == str(user_id):
            dice_streaks_sheet.update_cell(i + 2, 2, current)
            dice_streaks_sheet.update_cell(i + 2, 3, max_streak)
            return
    dice_streaks_sheet.append_row([str(user_id), current, max_streak])

@bot.tree.command(name="explore", description="探検に出かけよう！")
@app_commands.describe(minutes="探検時間を選んでね", location="探検場所")
async def explore(
    interaction: discord.Interaction,
    minutes: int,  # ← Choice[int] ではなく int に変更！
    location: Literal["森", "山", "川辺", "廃墟", "神社"]
):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)

    if get_explore_status(user_id):
        await interaction.followup.send("🧭 すでに探検中だよ！")
        return

    now = datetime.datetime.utcnow()
    end_time = now + datetime.timedelta(minutes=minutes)
    set_explore_status(user_id, end_time.isoformat(), minutes, location)

    duration_label = f"{minutes}分" if minutes < 60 else f"{minutes // 60}時間"
    msg = f"{EXPLORE_LOCATIONS[location]['emoji']} **{location}** へ {duration_label}の探検に出発したよ！\n⏳ 終了予定: <t:{int(end_time.timestamp())}:R>"

    if EXPLORE_OPTIONS[minutes]["charm"] and get_super_charm_count(user_id) > 0:
        set_super_charm_count(user_id, get_super_charm_count(user_id) - 1)
        set_super_charm_active(user_id, True)
        msg += "\n🌟 スーパーチャームを使って報酬が2倍になるよ！"

    await interaction.followup.send(msg)

@explore.autocomplete("minutes")
async def explore_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[int]]:
    choices = [
        app_commands.Choice(name="5分", value=5),
        app_commands.Choice(name="15分", value=15),
        app_commands.Choice(name="30分", value=30),
        app_commands.Choice(name="1時間", value=60),
        app_commands.Choice(name="2時間", value=120),
        app_commands.Choice(name="3時間", value=180),
        app_commands.Choice(name="6時間", value=360),
    ]
    return [c for c in choices if current in c.name]

@bot.tree.command(name="collect_explore", description="探検の報酬を受け取るよ！")
async def collect_explore(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)

    status = get_explore_status(user_id)
    if not status:
        await interaction.followup.send("🧭 探検中じゃないみたい！まずは `/explore` で出発してね！")
        return

    end_time_str, minutes, location = status
    end_time = datetime.datetime.fromisoformat(end_time_str)
    now = datetime.datetime.utcnow()

    if now < end_time:
        remaining = (end_time - now).seconds // 60
        await interaction.followup.send(f"⏳ まだ探検中だよ！あと {remaining} 分待ってね！")
        return

    used_items.setdefault(user_id, {})  # 安全に初期化

    gained_exp = minutes
    exp_msg = ""

    if used_items[user_id].get("光る石"):
        gained_exp = int(gained_exp * 1.5)
        used_items[user_id].pop("光る石")
        exp_msg += "\n💎 光る石の効果でEXPが1.5倍になったよ！"

    if used_items[user_id].get("祈りの結晶"):
        gained_exp = int(gained_exp * 1.5)
        used_items[user_id].pop("祈りの結晶")
        exp_msg += "\n🔮 祈りの結晶の効果でEXPがさらに1.5倍になったよ！"

    if used_items[user_id].get("神秘の面"):
        gained_exp = int(gained_exp * 1.2)
        used_items[user_id].pop("神秘の面")
        exp_msg += "\n🦊🔥 神秘の面の効果でEXPが1.2倍になったよ！"

    level = add_explore_exp(user_id, gained_exp)
    bonus_rate, charm_rate = get_explore_bonus(level)

    charm_msg = ""

    if used_items[user_id].get("ぬれたお守り"):
        charm_rate += 0.2
        used_items[user_id].pop("ぬれたお守り")
        charm_msg += "\n💧 ぬれたお守りの効果でスーパーチャーム発見率がアップしてるよ！"

    if used_items[user_id].get("祈りの結晶"):
        charm_rate += 0.2
        used_items[user_id].pop("祈りの結晶")
        charm_msg += "\n🔮 祈りの結晶の効果で発見率がさらにアップしてるよ！"

    if used_items[user_id].get("幻のエリア"):
        used_items[user_id].pop("幻のエリア")
        EXPLORE_LOCATIONS["星降る丘"] = {
            "emoji": "🌌",
            "bonus": 2.0,
        }
        location = "星降る丘"

    if used_items[user_id].get("幻のキツネ"):
        used_items[user_id].pop("幻のキツネ")
        add_user_item(user_id, "狐の面")
        charm_msg += "\n🦊✨ 幻のキツネが現れて、狐の面を1つくれたよ！"

    save_json(USED_ITEM_FILE, used_items)

    config = EXPLORE_OPTIONS[minutes]
    base_reward = random.randint(config["min"], config["max"])
    location_bonus = EXPLORE_LOCATIONS[location]["bonus"]
    reward = int(base_reward * bonus_rate * location_bonus)

    if is_super_charm_active(user_id):
        reward *= 2
        set_super_charm_active(user_id, False)
        charm_msg += "\n🌟 スーパーチャームの効果で報酬が2倍になったよ！"

    coins = get_coins(user_id) + reward
    set_coins(user_id, coins)

    msg = f"🎒 探検から帰ってきたよ！{reward}コインを見つけた！\n🧭 探検EXP +{gained_exp}（Lv.{level}){charm_msg}{exp_msg}"

    drop_item = EXPLORE_LOCATIONS[location]["drop"]
    drop_chance = 0.12  # デフォルト

    if minutes == 180:
        drop_chance = 0.5
    elif minutes == 360:
        drop_chance = 1.0

    if random.random() < drop_chance:
        add_user_item(user_id, drop_item)
        msg += f"\n🎁 特殊ドロップ発見！**{drop_item}** を手に入れた！（所持数 +1）"

    clear_explore_status(user_id)
    msg += f"\n💰 現在のコイン残高: {coins}"
    await interaction.followup.send(msg)
@bot.tree.command(name="use_item", description="特殊アイテムを使うよ！")
@app_commands.describe(item_name="使いたいアイテムの名前")
async def use_item(interaction: discord.Interaction, item_name: Literal[*SPECIAL_ITEMS]):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    items = get_user_items(user_id)

    if items.get(item_name, 0) <= 0:
        await interaction.followup.send(f"❌ {item_name} を持っていないよ！")
        return

    if item_name in ["木の実のかご", "特製おやつ"]:
        status = get_explore_status(user_id)
        if not status:
            await interaction.followup.send("🧭 探検中じゃないと使えないよ！")
            return

        end_time_str, minutes, location = status
        end_time = datetime.datetime.fromisoformat(end_time_str)
        now = datetime.datetime.utcnow()

        reduction = 15 if item_name == "木の実のかご" else 60
        new_end_time = end_time - datetime.timedelta(minutes=reduction)
        if new_end_time < now:
            new_end_time = now + datetime.timedelta(seconds=5)

        set_explore_status(user_id, new_end_time.isoformat(), minutes, location)
        add_user_item(user_id, item_name, -1)
        await interaction.followup.send(f"{ITEM_DESCRIPTIONS[item_name]['emoji']} {item_name} を使って、探検時間を短縮したよ！")
        return

    if item_name in ["光る石", "ぬれたお守り", "祈りの結晶", "神秘の面"]:
        used_items.setdefault(user_id, {})
        used_items[user_id][item_name] = True
        save_json(USED_ITEM_FILE, used_items)
        add_user_item(user_id, item_name, -1)
        await interaction.followup.send(f"{ITEM_DESCRIPTIONS[item_name]['emoji']} {item_name} を使ったよ！次の探検に効果があるよ！")
        return

    if item_name == "古びたコイン":
        add_user_item(user_id, item_name, -1)
        coins = get_coins(user_id) + 100
        set_coins(user_id, coins)
        await interaction.followup.send("🪙 古びたコインを使って、100コインを手に入れたよ！")
        return

    if item_name == "金のコイン":
        add_user_item(user_id, item_name, -1)
        coins = get_coins(user_id) + 500
        set_coins(user_id, coins)
        await interaction.followup.send("🪙✨ 金のコインを使って、500コインを手に入れたよ！")
        return

    if item_name == "狐の面":
        add_user_item(user_id, item_name, -1)
        count = get_super_charm_count(user_id) + 1
        set_super_charm_count(user_id, count)
        await interaction.followup.send("🦊 狐の面を使って、スーパーチャームを1個手に入れたよ！")
        return

    await interaction.followup.send("❓ そのアイテムはまだ使えないみたい…")
@bot.tree.command(name="combine_items", description="特殊アイテムを合成して強力なアイテムを作ろう！")
@app_commands.describe(target="作りたいアイテムを選んでね")
async def combine_items(
    interaction: discord.Interaction,
    target: str  # ← 修正済み！
):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    items = get_user_items(user_id)
    item_name = target  # ← そのまま使える！

    if item_name == "祈りの結晶":
        if items.get("光る石", 0) >= 1 and items.get("ぬれたお守り", 0) >= 1:
            add_user_item(user_id, "光る石", -1)
            add_user_item(user_id, "ぬれたお守り", -1)
            add_user_item(user_id, "祈りの結晶", 1)
            await interaction.followup.send("🔮 光る石 + ぬれたお守り → **祈りの結晶** を合成したよ！")
        else:
            await interaction.followup.send("素材が足りないよ！")

    elif item_name == "神秘の面":
        if items.get("狐の面", 0) >= 2 and items.get("祈りの結晶", 0) >= 1:
            add_user_item(user_id, "狐の面", -2)
            add_user_item(user_id, "祈りの結晶", -1)
            add_user_item(user_id, "神秘の面", 1)
            await interaction.followup.send("🦊🔥 狐の面×2 + 祈りの結晶 → **神秘の面** を合成したよ！")
        else:
            await interaction.followup.send("素材が足りないよ！")

    elif item_name == "特製おやつ":
        if items.get("木の実のかご", 0) >= 3:
            add_user_item(user_id, "木の実のかご", -3)
            add_user_item(user_id, "特製おやつ", 1)
            await interaction.followup.send("🧺🧺🧺 木の実のかご ×3 → **特製おやつ** を合成したよ！")
        else:
            await interaction.followup.send("素材が足りないよ！")

    elif item_name == "金のコイン":
        if items.get("古びたコイン", 0) >= 3:
            add_user_item(user_id, "古びたコイン", -3)
            add_user_item(user_id, "金のコイン", 1)
            await interaction.followup.send("🪙🪙🪙 古びたコイン ×3 → **金のコイン** を合成したよ！")
        else:
            await interaction.followup.send("素材が足りないよ！")

@combine_items.autocomplete("target")
async def combine_items_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    choices = [
        app_commands.Choice(name="祈りの結晶（光る石×1 + ぬれたお守り×1）", value="祈りの結晶"),
        app_commands.Choice(name="神秘の面（祈りの結晶×1 + 狐の面×2）", value="神秘の面"),
        app_commands.Choice(name="特製おやつ（木の実のかご×3）", value="特製おやつ"),
        app_commands.Choice(name="金のコイン（古びたコイン×3）", value="金のコイン"),
    ]
    return [c for c in choices if current in c.name]

@bot.tree.command(name="slot", description="スロットマシンを回してコインを賭けよう！")
async def slot(interaction: discord.Interaction, bet: int):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    coins = get_coins(user_id)

    if bet <= 0 or bet > coins:
        await interaction.followup.send("⚠️ 賭け金が無効か、コインが足りないよ！")
        return

    add_spent_coins(user_id, bet)

    roll = [random.choice(SLOTS) for _ in range(3)]
    counts = {symbol: roll.count(symbol) for symbol in set(roll)}
    star_count = roll.count("⭐")

    if len(set(roll)) == 1:
        winnings = bet * 3
        result_text = f"🎉 ジャックポット！{winnings}コイン獲得！"
    elif star_count >= 2:
        winnings = bet  # 実質 +1倍（ベット額は後で引かれる）
        result_text = f"🌟 スターが2つ出た！{bet * 2}コイン獲得！"

    elif star_count == 1:
        winnings = 0
        result_text = f"⭐ スターが1つ出た！賭け金は返金されたよ！"
    elif any(count == 2 for count in counts.values()):
        refund = int(bet * 0.5)
        winnings = -bet + refund
        result_text = f"🔁 2つ一致！{refund}コイン返ってきたよ！"
    else:
        winnings = -bet
        result_text = f"😢 はずれ！{bet}コイン失ったよ…"

    coins += winnings
    set_coins(user_id, coins)

    await interaction.followup.send(
        f"{' | '.join(roll)}\n{result_text}\n💰 現在のコイン残高: {coins}"
    )
@bot.tree.command(name="buy_charm", description="ラッキーチャームを購入するよ（300円）")
async def buy_charm(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    coins = get_coins(user_id)

    if coins < 300:
        await interaction.followup.send("💸 コインが足りないよ！")
        return

    coins -= 300
    set_coins(user_id, coins)

    # 💡 使用コインを記録！
    add_spent_coins(user_id, 300)

    current = get_charm_count(user_id)
    set_charm_count(user_id, current + 1)

    await interaction.followup.send(f"🧧 ラッキーチャームを1個購入したよ！現在の所持数：{current + 1}")

@bot.tree.command(name="daily", description="毎日コインをもらえるよ！")
async def daily(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)

    if has_received_bonus_today(user_id):
        await interaction.followup.send("🕒 今日はもうデイリーボーナスを受け取ったよ！また明日ね！")
        return

    today = datetime.datetime.utcnow().weekday()
    base_bonus = DAILY_BONUSES.get(today, 100)

    charm_count = get_charm_count(user_id)
    if charm_count > 0:
        bonus = int(base_bonus * 1.5)
        set_charm_count(user_id, charm_count - 1)
        charm_msg = f"🧧 ラッキーチャームの効果で報酬が1.5倍になったよ！（残り {charm_count - 1}個）\n"
    else:
        bonus = base_bonus
        charm_msg = ""

    coins = get_coins(user_id) + bonus
    set_coins(user_id, coins)
    set_bonus_date(user_id)

    weekday_names = ["月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"]
    weekday_name = weekday_names[today]

    await interaction.followup.send(
        f"{charm_msg}🎁 {weekday_name}のデイリーボーナス！{bonus}コインをゲットしたよ！\n💰 現在のコイン残高: {coins}"
    )

@bot.tree.command(name="deposit", description="銀行にコインを預けるよ")
async def deposit(interaction: discord.Interaction, amount: int):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    coins = get_coins(user_id)
    bank = get_bank(user_id)

    if amount <= 0 or coins < amount:
        await interaction.followup.send("⚠️ 金額が無効か、コインが足りないよ！")
        return

    coins -= amount
    bank += amount
    set_coins(user_id, coins)
    set_bank(user_id, bank)

    await interaction.followup.send(f"🏦 {amount}コインを銀行に預けたよ！\n💰 残高: {coins} / 銀行: {bank}")

@bot.tree.command(name="withdraw", description="銀行からコインを引き出すよ")
async def withdraw(interaction: discord.Interaction, amount: int):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    coins = get_coins(user_id)
    bank = get_bank(user_id)

    if amount <= 0 or bank < amount:
        await interaction.followup.send("⚠️ 金額が無効か、預金が足りないよ！")
        return

    bank -= amount
    coins += amount
    set_coins(user_id, coins)
    set_bank(user_id, bank)

    await interaction.followup.send(f"💸 {amount}コインを引き出したよ！\n💰 残高: {coins} / 銀行: {bank}")

@bot.tree.command(name="bank", description="銀行の預金残高を確認するよ")
async def bank(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    apply_weekly_interest()
    user_id = str(interaction.user.id)
    bank = get_bank(user_id)
    await interaction.followup.send(f"🏦 あなたの銀行預金残高は **{bank}コイン** だよ！")

@bot.tree.command(name="balance", description="自分のコイン残高を確認するよ")
async def balance(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    coins = get_coins(user_id)
    await interaction.followup.send(f"💰 あなたのコイン残高は {coins} コインだよ！")
@bot.tree.command(name="items", description="所持している特殊アイテムを確認するよ")
async def items(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    items = get_user_items(user_id)

    if all(count == 0 for count in items.values()):
        await interaction.followup.send("📦 特殊アイテムはまだ持っていないみたい！探検で見つけてみよう！")
        return

    item_emojis = {name: ITEM_DESCRIPTIONS[name]["emoji"] for name in SPECIAL_ITEMS}
    lines = [
        f"{item_emojis.get(name, '📦')} **{name}**：{count}個"
        for name, count in items.items() if count > 0
    ]
    msg = "🎒 あなたの特殊アイテム一覧：\n" + "\n".join(lines)
    await interaction.followup.send(msg)

@bot.tree.command(name="item_info", description="特殊アイテムの効果を確認するよ")
@app_commands.describe(item_name="調べたいアイテムの名前（省略すると一覧表示）")
async def item_info(
    interaction: discord.Interaction,
    item_name: Optional[Literal[*SPECIAL_ITEMS]] = None
):
    await interaction.response.defer(thinking=True)

    if item_name:
        info = ITEM_DESCRIPTIONS.get(item_name)
        if not info:
            await interaction.followup.send("そのアイテムの情報は見つからなかったよ…")
            return
        emoji = info["emoji"]
        description = info["description"]
        recipe = info.get("recipe")

        msg = f"{emoji} **{item_name}** の効果：\n{description}"
        if recipe:
            msg += "\n🧪 **合成レシピ**：\n" + "\n".join(f"・{r}" for r in recipe)

        await interaction.followup.send(msg)

    else:
        lines = []
        for name, data in ITEM_DESCRIPTIONS.items():
            line = f"{data['emoji']} **{name}**：{data['description']}"
            if "recipe" in data:
                line += "\n　🧪 合成レシピ：" + "、".join(data["recipe"])
            lines.append(line)

        msg = "📚 **特殊アイテムの効果一覧**：\n" + "\n".join(lines)
        await interaction.followup.send(msg)


@bot.tree.command(name="explore_level", description="探検レベルと経験値を確認するよ！")
async def explore_level_check(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    level, exp = get_explore_data(user_id)
    required = get_required_exp(level)
    await interaction.followup.send(f"🧭 探検レベル: Lv.{level}\n📈 経験値: {exp} / {required}")

@bot.tree.command(name="dice_guess", description="サイコロの合計が偶数か奇数かを当てよう！")
@app_commands.describe(guess="偶数か奇数を選んでね")
async def dice_guess(interaction: discord.Interaction, guess: Literal["偶数", "奇数"]):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    coins = get_coins(user_id)

    if coins > 100:
        await interaction.followup.send("🦊 このゲームはコインが100以下のときだけ遊べるよ！")
        return

    now = datetime.datetime.utcnow()
    cooldowns = load_json("dice_cooldowns.json")
    last_play = cooldowns.get(user_id)

    if last_play:
        last_time = datetime.datetime.fromisoformat(last_play)
        if (now - last_time).total_seconds() < 30:
            remaining = 30 - int((now - last_time).total_seconds())
            await interaction.followup.send(f"⏳ クールタイム中だよ！あと {remaining} 秒待ってね！")
            return

    die1 = random.randint(1, 6)
    die2 = random.randint(1, 6)
    total = die1 + die2
    result = "偶数" if total % 2 == 0 else "奇数"

    cooldowns[user_id] = now.isoformat()
    save_json("dice_cooldowns.json", cooldowns)

    current, max_streak = get_dice_streak(user_id)

    if guess == result:
        current += 1
        if current > max_streak:
            max_streak = current
        reward = 10 * current
        coins += reward
        set_coins(user_id, coins)
        msg = (
            f"🎉 正解！サイコロの目は {die1} と {die2}（合計 {total} → {result}）\n"
            f"🔥 連勝数: {current}（最高記録: {max_streak}） → 報酬 {reward}コイン！\n"
            f"💰 現在の残高: {coins}"
        )
    else:
        current = 0
        msg = (
            f"😢 残念！サイコロの目は {die1} と {die2}（合計 {total} → {result}）\n"
            f"💨 連勝がリセットされちゃった…（最高記録: {max_streak}）"
        )

    set_dice_streak(user_id, current, max_streak)
    await interaction.followup.send(msg)
@bot.tree.command(name="rank", description="自分のランクを確認するよ！")
async def rank(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    user_id = str(interaction.user.id)
    spent = get_spent_coins(user_id)
    rank = get_user_rank(user_id)

    is_vip = rank == "👑 VIP"
    await update_vip_role(interaction.user, is_vip)

    await interaction.followup.send(
        f"💰 累計使用コイン: {spent}\n🥇 あなたのランクは **{rank}** だよ！"
    )

@bot.tree.command(name="blackjack", description="ブラックジャックで遊ぼう！")
@app_commands.describe(bet="賭けるコインの数（例：100）")
async def blackjack(interaction: discord.Interaction, bet: int):
    await interaction.response.defer(thinking=True)

    user_id = str(interaction.user.id)  # ← 統一
    coins = get_coins(user_id)

    if bet <= 0:
        await interaction.followup.send("⚠️ ベット額は1以上にしてね！")
        return
    if bet > coins:
        await interaction.followup.send("💸 コインが足りないよ！")
        return

    set_coins(user_id, coins - bet)
    add_spent_coins(user_id, bet)

    player = [draw_card(), draw_card()]
    dealer = [draw_card(), draw_card()]

    sessions[user_id] = {  # ← str で統一
        "player": player,
        "dealer": dealer,
        "bet": bet,
        "has_hit": False
    }

    view = BlackjackView(user_id, bet)  # ← str で統一
    msg = (
        f"🃏 **ブラックジャック**\n\n"
        f"**あなた** | {hand_value(player)}\n{' '.join(player)}\n"
        f"**ディーラー** | ?\n{dealer[0]} ❓\n\n"
        f"👉 ヒット or スタンド？"
    )
    await interaction.followup.send(content=msg, view=view)

@bot.tree.command(name="shop", description="ショップを開くよ！")
async def shop(interaction: discord.Interaction):
    user_id = str(interaction.user.id)

    discount_item = choose_daily_discount()

    embed = discord.Embed(
        title="🛒 ショップ",
        description=f"今日の割引商品は **{discount_item}** だよ！ 50% OFF！",
        color=0x00ccff
    )

    for name, price in SHOP_ITEMS.items():
        emoji = ITEM_DESCRIPTIONS[name]["emoji"]

        if name == discount_item:
            embed.add_field(
                name=f"🔥 {emoji} {name}（今日だけ半額！）",
                value=f"~~{price}~~ → **{price // 2} コイン**",
                inline=False
            )
        else:
            embed.add_field(
                name=f"{emoji} {name}",
                value=f"{price} コイン",
                inline=False
            )

    view = ShopView(user_id)
    await interaction.response.send_message(embed=embed, view=view)
@bot.tree.command(name="highlow", description="ハイローで勝負しよう！")
@app_commands.describe(bet="賭けるコイン数")
async def highlow(interaction: discord.Interaction, bet: int):
    await interaction.response.defer(thinking=True)
    user_id = str(interaction.user.id)
    coins = get_coins(user_id)

    if bet <= 0:
        await interaction.followup.send("⚠️ 1コイン以上賭けてね！")
        return

    if coins < bet:
        await interaction.followup.send("💸 コインが足りないよ！")
        return

    # ベットを引く
    set_coins(user_id, coins - bet)
    add_spent_coins(user_id, bet)

    first_card = draw_card()
    first_val = card_value_highlow(first_card)
    odds = get_highlow_odds(first_val)

    sessions[user_id] = {
        "bet": bet,
        "current_card": first_card,
        "multiplier": 1.0
    }

    msg = (
        f"🎲 **ハイローゲーム**\n\n"
        f"最初のカード：{first_card}（{first_val}）\n"
        f"High 倍率：x{odds['high']}\n"
        f"Low 倍率：x{odds['low']}\n\n"
        "High or Low？"
    )

    await interaction.followup.send(content=msg, view=HighLowView(user_id))

@bot.tree.command(name="mine", description="マインで遊ぼう！")
@app_commands.describe(bet="賭けるコイン", bombs="爆弾の数（1〜19）")
async def mine(interaction: discord.Interaction, bet: int, bombs: int):
    await interaction.response.defer(thinking=True)

    user_id = str(interaction.user.id)
    coins = get_coins(user_id)

    if bet <= 0:
        await interaction.followup.send("⚠️ ベット額は1以上にしてね！")
        return
    if bet > coins:
        await interaction.followup.send("💸 コインが足りないよ！")
        return
    if bombs < 1 or bombs >= 20:
        await interaction.followup.send("⚠️ 爆弾の数は 1〜19 にしてね！")
        return

    # ベットを引く
    set_coins(user_id, coins - bet)
    add_spent_coins(user_id, bet)

    # 爆弾配置（20マス）
    bomb_positions = set(random.sample(range(20), bombs))

    sessions[user_id] = {
        "bet": bet,
        "bombs": bombs,
        "opened": set(),
        "bomb_positions": bomb_positions,
        "multiplier": 1.0
    }

    board = render_mines_board(sessions[user_id])

    view = MinesView(user_id)

    await interaction.followup.send(
        content=(
            f"💣 **Mines 開始！**\n"
            f"ベット：{bet}\n"
            f"爆弾：{bombs} 個\n\n"
            f"{board}\n\n"
            "開けるマスを選んでね！"
        ),
        view=view
    )

# --- Bot起動 ---
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}!")

bot.run(os.getenv("DISCORD_TOKEN"))