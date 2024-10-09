import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters.command import Command
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.enums import ChatType
from datetime import datetime, timedelta
from collections import defaultdict
import logging
import json
import os

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Токен бота
BOT_TOKEN = "YOUR_TOKEN"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Файл для хранения настроек уведомлений
ALERTS_FILE = "user_alerts.json"

# Загрузка уведомлений из файла
def load_alerts():
    if os.path.exists(ALERTS_FILE):
        with open(ALERTS_FILE, 'r') as f:
            alerts = json.load(f)
        # Конвертация строковых дат обратно в объекты datetime
        for alert in alerts.values():
            alert['last_check'] = datetime.fromisoformat(alert['last_check'])
        return alerts
    return {}

# Сохранение уведомлений в файл
def save_alerts():
    alerts_to_save = {str(k): v.copy() for k, v in user_alerts.items()}
    # Конвертация объектов datetime в строки для сериализации JSON
    for alert in alerts_to_save.values():
        alert['last_check'] = alert['last_check'].isoformat()
    with open(ALERTS_FILE, 'w') as f:
        json.dump(alerts_to_save, f)

# Загрузка уведомлений при запуске бота
user_alerts = load_alerts()

# Словарь для хранения информации о последних ответах на команду /apy
apy_messages = {}

# Проверка прав администратора
async def is_user_admin(message: types.Message):
    if message.chat.type == ChatType.PRIVATE:
        return True
    
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        logger.info(f"Message sent by anonymous group admin in chat {message.chat.id}")
        return True
    
    if not message.from_user:
        logger.info(f"No user information available for message in chat {message.chat.id}")
        return False
    
    try:
        chat_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        logger.info(f"Checking admin rights for user {message.from_user.id} in chat {message.chat.id}")
        logger.info(f"Member status: {chat_member.status}")
        
        if isinstance(chat_member, (ChatMemberOwner, ChatMemberAdministrator)):
            logger.info(f"User {message.from_user.id} is an admin or owner in chat {message.chat.id}")
            return True
        else:
            logger.info(f"User {message.from_user.id} is not an admin in chat {message.chat.id}")
            return False
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False
        
class AlertStates(StatesGroup):
    waiting_for_apy = State()
    waiting_for_interval = State()

async def fetch_data():
    async with aiohttp.ClientSession() as session:
        async with session.get('https://api.curve.fi/v1/getLendingVaults/all') as response:
            return await response.json()
  
# Проверка прав бота при добавлении в группу
@dp.my_chat_member()
async def on_bot_join(event: types.ChatMemberUpdated):
    if event.new_chat_member.status == "member":
        await event.chat.send_message("Спасибо за добавление меня в группу! Пожалуйста, назначьте меня администратором, чтобы я мог корректно работать.")

@dp.message(Command("apy"))
async def cmd_apy(message: types.Message):
    try:
        # Проверяем, указано ли число для выбора топ-N пулов
        command_parts = message.text.split()
        top_n = None
        if len(command_parts) > 1 and command_parts[1].isdigit():
            top_n = int(command_parts[1])

        data = await fetch_data()
        formatted_data = format_data(data, top_n)

        # Проверяем, является ли чат групповым
        if message.chat.type != ChatType.PRIVATE:
            current_time = datetime.now()
            chat_id = message.chat.id

            # Проверяем, есть ли предыдущее сообщение /apy в этом чате за последние 6 часов
            if chat_id in apy_messages:
                last_message_time, last_message_id = apy_messages[chat_id]
                if current_time - last_message_time < timedelta(hours=6):
                    # Удаляем предыдущее сообщение
                    try:
                        await bot.delete_message(chat_id, last_message_id)
                    except Exception as e:
                        logging.error(f"Error deleting previous message: {e}")

            # Отправляем новое сообщение
            new_message = await message.answer(formatted_data, parse_mode="HTML", disable_web_page_preview=True)
            
            # Сохраняем информацию о новом сообщении
            apy_messages[chat_id] = (current_time, new_message.message_id)

        else:
            # Для личных чатов просто отправляем сообщение
            await message.answer(formatted_data, parse_mode="HTML", disable_web_page_preview=True)

    except Exception as e:
        error_message = f"Произошла ошибка при получении данных: {str(e)}"
        await message.answer(error_message)

# Изменим функцию format_data для работы с параметром top_n
def format_data(data, top_n=None):
    networks = defaultdict(list)
    vaults = data.get('data', {}).get('lendingVaultData', [])

    if not vaults:
        return "Не удалось получить данные о пулах. Пожалуйста, попробуйте позже."

    found_any = False

    for vault in vaults:
        network = vault.get('blockchainId', 'Unknown')
        symbol = vault.get('assets', {}).get('collateral', {}).get('symbol', 'Unknown')
        lend_apy = vault.get('rates', {}).get('lendApyPcent', 0)
        
        # Собираем информацию о вознаграждениях (инсентивы)
        rewards_list = [(reward['apy'], reward['symbol']) for reward in vault.get('gaugeRewards', [])]
        rewards = sum([reward[0] for reward in rewards_list])  # Общая доходность инсентивов
        rewards_tokens = ', '.join([reward[1] for reward in rewards_list if reward[1]])  # Токены инсентивов

        total_apy = lend_apy + rewards  # Общая доходность
        url = vault.get('lendingVaultUrls', {}).get('deposit', '#')

        if total_apy >= 1:
            found_any = True
            networks[network].append((symbol, lend_apy, rewards, rewards_tokens, url, total_apy))

    formatted_output = "Доходность депозита crvUSD в Curve Lend (>= 1%)\n\n"
    for network, vault_list in networks.items():
        # Сортируем по общей доходности (total_apy) и берем top-N пулов, если указано
        vault_list = sorted(vault_list, key=lambda x: x[5], reverse=True)
        if top_n is not None:
            vault_list = vault_list[:top_n]

        if vault_list:
            formatted_output += f"{network.capitalize()}:\n"
            for symbol, apy, rewards, rewards_tokens, url, total_apy in vault_list:
                rocket = "\U0001F680 " if total_apy > 20 else ""
                reward_text = f"+ {rewards:.2f}% ({rewards_tokens}) " if rewards else ""
                formatted_output += f"<a href='{url}'>{symbol}</a>: {apy:.2f}% {reward_text}{rocket}\n"
            formatted_output += "\n"

    if not found_any:
        return "В настоящее время нет пулов с доходностью >= 1%."

    return formatted_output

# Команда /alert_add доступна только в личных сообщениях или для администраторов в группах
@dp.message(Command("alert_add"))
async def start_alert(message: types.Message, state: FSMContext):
    if await is_user_admin(message):
        await process_alert_add(message, state)
    else:
        await message.answer("Эта команда доступна только в личных сообщениях или администраторам группы.")
            
async def process_alert_add(message: types.Message, state: FSMContext):
    await message.answer("Введите желаемую доходность (от 10 до 50):")
    await state.set_state(AlertStates.waiting_for_apy)

# Шаг 1: Ввод доходности
@dp.message(AlertStates.waiting_for_apy)
async def process_apy(message: types.Message, state: FSMContext):
    try:
        apy = int(float(message.text))
        if 10 <= apy <= 50:
            await state.update_data(apy=apy)
            await message.answer("Введите периодичность проверки уведомлений (в часах):")
            await state.set_state(AlertStates.waiting_for_interval)
        else:
            await message.answer("Пожалуйста, введите число от 10 до 50.")
    except (ValueError, TypeError):
        await message.answer("Введите корректное число.")

# Шаг 2: Ввод интервала и тестовая проверка
@dp.message(AlertStates.waiting_for_interval)
async def process_interval(message: types.Message, state: FSMContext):
    try:
        interval = int(message.text)
        user_data = await state.get_data()
        apy = user_data['apy']
        user_id = message.from_user.id
        chat_id = message.chat.id
        
        user_alerts[(user_id, chat_id)] = {
            'apy': apy,
            'interval': interval,
            'last_check': datetime.now()
        }
        
        save_alerts()  # Save alerts after adding a new one

        await message.answer(f"Уведомление установлено:\nДоходность: {apy}%\nИнтервал: {interval} час(ов)")

        # Тестовая проверка
        await test_alert_check(message, apy)

        await state.clear()
    except ValueError:
        await message.answer("Введите корректное число для интервала.")

# Функция тестовой проверки с реальными данными
async def test_alert_check(message: types.Message, target_apy: int):
    data = await fetch_data()
    vaults = data.get('data', {}).get('lendingVaultData', [])
    
    if not vaults:
        await message.answer("Не удалось получить данные о пулах. Пожалуйста, попробуйте позже.")
        return
    
    networks = defaultdict(list)
    for vault in vaults:
        network = vault.get('blockchainId', 'Unknown')
        symbol = vault.get('assets', {}).get('collateral', {}).get('symbol', 'Unknown')
        lend_apy = vault.get('rates', {}).get('lendApyPcent', 0)
        rewards = vault.get('gaugeRewards', [])
        total_rewards_apy = sum([reward.get('apy', 0) for reward in rewards])
        reward_symbols = [reward.get('symbol', '') for reward in rewards if reward.get('apy', 0) > 0]
        url = vault.get('lendingVaultUrls', {}).get('deposit', '#')
        
        total_apy = lend_apy + total_rewards_apy
        
        if total_apy >= target_apy:
            if total_rewards_apy > 0:
                reward_symbols_str = f"({', '.join(reward_symbols)})" if reward_symbols else ""
                apy_str = f"{lend_apy:.2f}% + {total_rewards_apy:.2f}% {reward_symbols_str}"
            else:
                apy_str = f"{lend_apy:.2f}%"
            
            networks[network].append((symbol, apy_str, url))

    if networks:
        formatted_output = f"\U0001F6A8 Тестовое уведомление: Curve Lend crvUSD deposit APY (>= {target_apy}%)\n\n"
        for network, vault_list in networks.items():
            formatted_output += f"{network.capitalize()}:\n"
            for symbol, apy_str, url in sorted(vault_list, key=lambda x: float(x[1].split('%')[0]), reverse=True):
                rocket = "\U0001F680 " if float(apy_str.split('%')[0]) > 20 else ""
                formatted_output += f"<a href='{url}'>{symbol}</a>: {apy_str} {rocket}\n"
            formatted_output += "\n"
        await message.answer(formatted_output, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await message.answer(f"Нет пулов, соответствующих вашему целевому APY ({target_apy}%) в тестовой проверке.")



# Функция для реальной проверки уведомлений
async def check_alerts():
    while True:
        data = await fetch_data()
        vaults = data.get('data', {}).get('lendingVaultData', [])
        
        for (user_id, chat_id), alert in user_alerts.items():
            if datetime.now() - alert["last_check"] >= timedelta(hours=alert["interval"]):
                networks = defaultdict(list)
                
                for vault in vaults:
                    network = vault.get('blockchainId', 'Unknown')
                    symbol = vault.get('assets', {}).get('collateral', {}).get('symbol', 'Unknown')
                    lend_apy = vault.get('rates', {}).get('lendApyPcent', 0)
                    rewards = vault.get('gaugeRewards', [])
                    total_rewards_apy = sum([reward.get('apy', 0) for reward in rewards])
                    reward_symbols = [reward.get('symbol', '') for reward in rewards if reward.get('apy', 0) > 0]
                    url = vault.get('lendingVaultUrls', {}).get('deposit', '#')
                    
                    total_apy = lend_apy + total_rewards_apy
                    
                    if total_apy >= alert['apy']:
                        if total_rewards_apy > 0:
                            reward_symbols_str = f"({', '.join(reward_symbols)})" if reward_symbols else ""
                            apy_str = f"{lend_apy:.2f}% + {total_rewards_apy:.2f}% {reward_symbols_str}"
                        else:
                            apy_str = f"{lend_apy:.2f}%"
                        
                        networks[network].append((symbol, apy_str, url))

                if networks:
                    formatted_output = f"\U0001F6A8 Уведомление: Curve Lend crvUSD deposit APY (>= {alert['apy']}%)\n\n"
                    for network, vault_list in networks.items():
                        formatted_output += f"{network.capitalize()}:\n"
                        for symbol, apy_str, url in sorted(vault_list, key=lambda x: float(x[1].split('%')[0]), reverse=True):
                            rocket = "\U0001F680 " if float(apy_str.split('%')[0]) > 20 else ""
                            formatted_output += f"<a href='{url}'>{symbol}</a>: {apy_str} {rocket}\n"
                        formatted_output += "\n"
                    await bot.send_message(user_id, formatted_output, parse_mode="HTML", disable_web_page_preview=True)
                
                # Обновляем время последней проверки
                alert["last_check"] = datetime.now()
                save_alerts()

        await asyncio.sleep(3600)  # Проверка каждый час
        
# Команда /alert_cancel
@dp.message(Command("alert_cancel"))
async def cancel_alert(message: types.Message):
    if message.chat.type == ChatType.PRIVATE:
        await process_alert_cancel(message)
    else:
        if await is_user_admin(message):
            await process_alert_cancel(message)
        else:
            await message.answer("Эта команда доступна только администраторам группы.")

async def process_alert_cancel(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    
    alert_key = (user_id, chat_id)
    if alert_key in user_alerts:
        del user_alerts[alert_key]
        save_alerts()  # Сохраняем уведомления после удаления
        await message.answer("Все уведомления были успешно отменены.")
    else:
        await message.answer("У вас нет активных уведомлений.")

@dp.message(Command("alert_add", "alert_cancel"))
async def log_command(message: types.Message):
    print(f"Command received: {message.text}")
    print(f"User ID: {message.from_user.id}")
    print(f"Chat ID: {message.chat.id}")
    print(f"Chat type: {message.chat.type}")
    if message.chat.type != ChatType.PRIVATE:
        is_admin = await is_user_admin(message)
        print(f"Is admin: {is_admin}")

async def main():
    logger.info("Starting the bot")
    asyncio.create_task(check_alerts())
    await dp.start_polling(bot)

if __name__ == "__main__":
    logger.info("Script started")
    asyncio.run(main())
