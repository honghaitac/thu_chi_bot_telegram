import asyncio
import traceback
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
import google.generativeai as genai

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from telebot.async_telebot import AsyncTeleBot
from telebot.types import Message

from md2tgmd import escape
from unidecode import unidecode  # Thư viện để chuẩn hóa (bỏ dấu) chuỗi

# Load environment variables from .env file
load_dotenv()

tg_token = os.getenv("TELEGRAM_BOT_API_KEY")
gemini_key = os.getenv("GOOGLE_GEMINI_API_KEY")
sheet_id = os.getenv("GOOGLE_SHEET_ID")
service_account_json = os.getenv("SERVICE_ACCOUNT_JSON")

if not tg_token or not gemini_key or not sheet_id or not service_account_json:
    raise ValueError("Chưa cấu hình đủ biến môi trường trong file .env.")

# Cấu hình Gemini
genai.configure(api_key=gemini_key)

# Khởi tạo bot
bot = AsyncTeleBot(tg_token)

model_2 = "gemini-2.0-flash-exp"
generation_config = {
    "temperature": 1,
    "top_p": 1,
    "top_k": 1,
    "max_output_tokens": 1024,
}
safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]
gemini_player_dict = {}

# Google Sheets setup
def setup_google_sheets(json_keyfile: str, sheet_id: str, worksheet_name: str = None):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(json_keyfile, scope)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(sheet_id)
    sheet = spreadsheet.worksheet(worksheet_name) if worksheet_name else spreadsheet.sheet1
    print(f"Đã kết nối Google Sheet: {spreadsheet.title}, Worksheet: {sheet.title}")
    return sheet

def parse_date(date_string):
    formats = ["%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"]
    for fmt in formats:
        try:
            return datetime.strptime(date_string, fmt)
        except ValueError:
            continue
    raise ValueError(f"Date '{date_string}' không khớp với các định dạng: {formats}")

def get_column_indices(sheet):
    header_row = sheet.row_values(1)
    header_lower = [unidecode(h.strip().lower()) for h in header_row]

    date_index = header_lower.index("date") + 1
    category_index = header_lower.index("category") + 1
    amount_index = header_lower.index("amount") + 1

    return date_index, category_index, amount_index

def calculate_total(sheet, user_category, start_date, end_date):
    date_col, cat_col, amt_col = get_column_indices(sheet)
    data = sheet.get_all_values()

    total = 0
    for row in data[1:]:
        try:
            record_date_str = row[date_col - 1]
            record_category = row[cat_col - 1]
            record_amount_str = row[amt_col - 1]

            record_date = parse_date(record_date_str)

            if (start_date <= record_date <= end_date and
                unidecode(record_category.strip().lower()) == unidecode(user_category.strip().lower())):
                
                amt_cleaned = record_amount_str.replace(".", "")
                amount = float(amt_cleaned)
                total += amount

        except Exception as e:
            print(f"Bỏ qua dòng do lỗi: {e}")
    return total

async def gemini(bot, message, m, model_type):
    player = gemini_player_dict.get(str(message.from_user.id))
    if not player:
        player = genai.GenerativeModel(
            model_name=model_type,
            generation_config=generation_config,
            safety_settings=safety_settings,
        ).start_chat()
        gemini_player_dict[str(message.from_user.id)] = player

    if len(player.history) > 30:
        player.history = player.history[2:]

    try:
        sent_message = await bot.reply_to(message, "🤖 Đang tạo nội dung...")
        player.send_message(m)
        await bot.edit_message_text(
            escape(player.last.text),
            chat_id=sent_message.chat.id,
            message_id=sent_message.message_id,
            parse_mode="MarkdownV2",
        )
    except Exception:
        traceback.print_exc()
        await bot.edit_message_text(
            "⚠️ Đã xảy ra lỗi! Vui lòng thử lại.",
            chat_id=sent_message.chat.id,
            message_id=sent_message.message_id
        )

@bot.message_handler(commands=["start"])
async def handle_start(message: Message):
    await bot.reply_to(
        message,
        "Chào mừng! Bạn có thể gõ lệnh /analyze <danh_mục> <thời_gian>, ví dụ: /analyze cafe tuần này, hoặc /analyze nhậu tháng này."
    )

@bot.message_handler(commands=["analyze"])
async def handle_analyze(message: Message):
    try:
        text = message.text.strip().split(maxsplit=2)
        if len(text) < 3:
            await bot.reply_to(message, "Hãy dùng cú pháp: /analyze <danh_mục> <thời_gian>\nVí dụ: /analyze cafe tuần này")
            return

        category = text[1]
        time_period = text[2].lower()

        sheet = setup_google_sheets(service_account_json, sheet_id)
        today = datetime.today()

        if "tuần" in time_period:
            start_date = today - timedelta(days=today.weekday())
            end_date = today
        elif "tháng" in time_period:
            start_date = today.replace(day=1)
            end_date = today
        else:
            await bot.reply_to(message, "Thời gian không rõ. Hãy thử 'tuần này' hoặc 'tháng này'.")
            return

        total = calculate_total(sheet, category, start_date, end_date)

        model = genai.GenerativeModel(model_name=model_2)

        # Prompt tiếng Việt
        prompt_text = (
            f"Hãy trả lời bằng tiếng Việt.\n\n"
            f"Tổng chi tiêu cho '{category}' từ "
            f"{start_date.strftime('%Y-%m-%d')} đến "
            f"{end_date.strftime('%Y-%m-%d')} là {total:.2f} VND."
        )
        response = model.generate_content(prompt_text)

        await bot.reply_to(message, response.text)

    except Exception as e:
        traceback.print_exc()
        await bot.reply_to(message, f"Đã xảy ra lỗi khi xử lý yêu cầu. Vui lòng thử lại!\nChi tiết: {e}")

@bot.message_handler(func=lambda message: message.chat.type == "private", content_types=["text"])
async def handle_questions(message: Message):
    m = message.text.strip()
    await gemini(bot, message, m, model_2)

async def main():
    print("Bot đang khởi chạy...")
    await bot.polling(none_stop=True)

if __name__ == "__main__":
    asyncio.run(main())
