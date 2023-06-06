import argparse
import configparser
import logging
import time
from logging.handlers import RotatingFileHandler

import discord
import openai
from discord.errors import ConnectionClosed

# Parse command-line arguments
parser = argparse.ArgumentParser(description='GPT-based Discord bot.')
parser.add_argument('--conf', help='Configuration file path')
args = parser.parse_args()

# Read configuration from a file
config = configparser.ConfigParser()
config.read(args.conf)

# Retrieve configuration details from the configuration file
DISCORD_TOKEN = config.get('Discord', 'DISCORD_TOKEN')
ALLOWED_CHANNELS = config.get('Discord', 'ALLOWED_CHANNELS', fallback='').split(',')
BOT_PRESENCE = config.get('Discord', 'BOT_PRESENCE', fallback='online')

# ACTIVITY_TYPE options: playing, streaming, listening, watching, custom, competing
ACTIVITY_TYPE = config.get('Discord', 'ACTIVITY_TYPE', fallback='listening')
ACTIVITY_STATUS = config.get('Discord', 'ACTIVITY_STATUS', fallback='Humans')

OPENAI_API_KEY = config.get('OpenAI', 'OPENAI_API_KEY')
OPENAI_TIMEOUT = config.get('OpenAI', 'OPENAI_TIMEOUT', fallback='30')
GPT_MODEL = config.get('OpenAI', 'GPT_MODEL', fallback='gpt-3.5-turbo')
GPT_TOKENS = config.getint('OpenAI', 'GPT_TOKENS', fallback=60)
SYSTEM_MESSAGE = config.get('OpenAI', 'SYSTEM_MESSAGE', fallback='You are a helpful assistant.')

RATE_LIMIT = config.getint('Limits', 'RATE_LIMIT', fallback=10)
RATE_LIMIT_PER = config.getint('Limits', 'RATE_LIMIT_PER', fallback=60)

LOG_FILE = config.get('Logging', 'LOG_FILE', fallback='bot.log')

# Set up logging
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)

# File handler
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5)
file_handler.setLevel(logging.WARNING)
file_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Set the intents for the bot
intents = discord.Intents.default()
intents.typing = False
intents.presences = False

# Set your OpenAI API key
openai.api_key = OPENAI_API_KEY

# Create a dictionary to store the last command timestamp for each user
last_command_timestamps = {}
last_command_count = {}

# Create a dictionary to store conversation history for each user
conversation_history = {}

def set_activity_status(activity_type, activity_status):
    """
    Returns a discord.Activity object with the specified activity type and status.
    """
    activity_types = {
        'playing': discord.ActivityType.playing,
        'streaming': discord.ActivityType.streaming,
        'listening': discord.ActivityType.listening,
        'watching': discord.ActivityType.watching,
        'custom': discord.ActivityType.custom,
        'competing': discord.ActivityType.competing
    }
    activity_type = activity_types.get(activity_type, discord.ActivityType.listening)
    return discord.Activity(type=activity_type, name=activity_status)

# Set the bot's presence and activity status
activity = set_activity_status(config['Discord']['ACTIVITY_TYPE'], config['Discord']['ACTIVITY_STATUS'])
bot = discord.Client(intents=intents, activity=activity, status=discord.Status(BOT_PRESENCE))

@bot.event
async def on_ready():
    """
    Event handler for when the bot is ready to receive messages.
    """
    logger.info(f'We have logged in as {bot.user}')
    logger.info(f'Configured bot presence: {BOT_PRESENCE}')
    logger.info(f'Configured activity type: {ACTIVITY_TYPE}')
    logger.info(f'Configured activity status: {ACTIVITY_STATUS}')


@bot.event
async def on_message(message):
    """
    Event handler for when a message is received.
    """
    if message.author == bot.user:
        return

    if isinstance(message.channel, discord.DMChannel):
        # Process DM messages without the @botname requirement
        logger.info(f'Received DM: {message.content} | Author: {message.author}')

        if not await check_rate_limit(message.author):
            await message.channel.send("Command on cooldown. Please wait before using it again.")
            return

        conversation_summary = get_conversation_summary(conversation_history.get(message.author.id, []))
        response = await process_input_message(message.content, message.author, conversation_summary)
        await message.channel.send(response)
    elif isinstance(message.channel, discord.TextChannel) and message.channel.name in ALLOWED_CHANNELS:
        if bot.user in message.mentions:
            logger.info(f'Received message: {message.content} | Channel: {message.channel} | Author: {message.author}')

            if not await check_rate_limit(message.author):
                await message.channel.send("Command on cooldown. Please wait before using it again.")
                return

            conversation_summary = get_conversation_summary(conversation_history.get(message.author.id, []))
            response = await process_input_message(message.content, message.author, conversation_summary)
            await message.channel.send(response)


async def check_rate_limit(user):
    """
    Check if a user has exceeded the rate limit for sending messages.
    """
    current_time = time.time()
    last_command_timestamp = last_command_timestamps.get(user.id, 0)
    last_command_count_user = last_command_count.get(user.id, 0)

    if current_time - last_command_timestamp > RATE_LIMIT_PER:
        last_command_timestamps[user.id] = current_time
        last_command_count[user.id] = 1
        logger.info(f"Rate limit passed for user: {user}")
        return True

    if last_command_count_user < RATE_LIMIT:
        last_command_count[user.id] += 1
        logger.info(f"Rate limit passed for user: {user}")
        return True

    logger.info(f"Rate limit exceeded for user: {user}")
    return False


async def process_input_message(input_message, user, conversation_summary):
    """
    Process an input message using OpenAI's GPT model.
    """
    try:
        logger.info("Sending prompt to OpenAI API...")

        conversation = conversation_history.get(user.id, [])
        conversation.append({"role": "user", "content": input_message})

        conversation_tokens = sum(len(message["content"].split()) for message in conversation)

        if conversation_tokens >= GPT_TOKENS * 0.8:
            conversation_summary = get_conversation_summary(conversation)
            conversation_tokens_summary = sum(len(message["content"].split()) for message in conversation_summary)
            max_tokens = GPT_TOKENS - conversation_tokens_summary
        else:
            max_tokens = GPT_TOKENS - conversation_tokens

        logger.info("Current conversation history:")
        logger.info(conversation)  # Log the current conversation history

        response = openai.ChatCompletion.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_MESSAGE},
                *conversation_summary,
                {"role": "user", "content": input_message}
            ],
            max_tokens=max_tokens,
            timeout=OPENAI_TIMEOUT
        )

        if "choices" in response and response.choices:
            response_content = response.choices[0].message.content.strip()
            logger.info("Received response from OpenAI API.")
            logger.info(f"Sent the response: {response_content}")

            conversation.append({"role": "assistant", "content": response_content})
            conversation_history[user.id] = conversation

            return response_content
        else:
            logger.error("Failed to get response from OpenAI API.")
            return "Sorry, an error occurred while processing the message."

    except ConnectionClosed as error:
        logger.error(f"WebSocket connection closed: {error}")
        logger.info("Reconnecting in 5 seconds...")
        await asyncio.sleep(5)
        await bot.login(DISCORD_TOKEN)
        await bot.connect(reconnect=True)
    except openai.api_error.ApiError as error:
        logger.error(f"An error occurred during OpenAI API call: {error}")
        return "Sorry, an error occurred while processing the message."
    except Exception as error:
        logger.error(f"An error occurred while processing the message: {error}")
        return "Sorry, an error occurred while processing the message."


def get_conversation_summary(conversation):
    """
    Get a summary of the conversation by combining user messages and assistant responses.
    """
    summary = []
    user_messages = [message for message in conversation if message["role"] == "user"]
    assistant_responses = [message for message in conversation if message["role"] == "assistant"]

    # Combine user messages and assistant responses into a summary
    for user_message, assistant_response in zip(user_messages, assistant_responses):
        summary.append(user_message)
        summary.append(assistant_response)

    return summary


# Run the bot
bot.run(DISCORD_TOKEN)