import os
import sys
import traceback
import json
from typing import Any, Dict, Optional
from dataclasses import asdict

from botbuilder.core import MemoryStorage, TurnContext
from state import AppTurnState
from teams import Application, ApplicationOptions, TeamsAdapter
from teams.ai import AIOptions
from teams.ai.actions import ActionTurnContext
from teams.ai.models import AzureOpenAIModelOptions, OpenAIModel, OpenAIModelOptions
from teams.ai.planners import ActionPlanner, ActionPlannerOptions
from teams.ai.prompts import PromptManager, PromptManagerOptions
from teams.state import TurnState
from teams.feedback_loop_data import FeedbackLoopData
from botbuilder.schema import Mention, Activity
from typing import List
import json
from pathlib import Path
import tiktoken

from config import Config

config = Config()

# Token management constants
MAX_HISTORY_TOKENS = 4000
SAFETY_BUFFER_TOKENS = 100

enc = tiktoken.get_encoding("cl100k_base")

# Create AI components
model: OpenAIModel

model = OpenAIModel(
    OpenAIModelOptions(
        api_key=config.OPENAI_API_KEY,
        default_model=config.OPENAI_MODEL_NAME,
    )
)
    
prompts = PromptManager(PromptManagerOptions(prompts_folder=f"{os.getcwd()}/prompts"))

planner = ActionPlanner(
    ActionPlannerOptions(model=model, prompts=prompts, default_prompt="planner")
)

# Define storage and application
storage = MemoryStorage()
bot_app = Application[AppTurnState](
    ApplicationOptions(
        bot_app_id=config.APP_ID,
        storage=storage,
        adapter=TeamsAdapter(config),
        ai=AIOptions(planner=planner, enable_feedback_loop=True),
    )
)

def is_bot_mentioned(context: TurnContext) -> bool:
    bot_id = (context.activity.recipient.id or "").lower()
    mentions = TurnContext.get_mentions(context.activity)

    for m in mentions:
        mentioned_data = None
        if hasattr(m, "additional_properties"):
            mentioned_data = m.additional_properties.get("mentioned")
        
        if mentioned_data and (mentioned_data.get("id") or "").lower() == bot_id:
            return True

    return False


@bot_app.turn_state_factory
async def turn_state_factory(context: TurnContext):
    return await AppTurnState.load(context, storage)

@bot_app.activity("message")
async def on_message_activity(context: TurnContext, state: AppTurnState):
    """
    Stores all messages but only allows bot responses when mentioned.
    """

    if not hasattr(state.conversation, 'all_messages'):
        state.conversation.all_messages = []
    
    if not hasattr(state.conversation, 'history_snippet'):
        state.conversation.history_snippet = []

        if not hasattr(state.conversation, 'history_snippet_tokens'):
            state.conversation.history_snippet_tokens = 0

    # Store the current message
    message_data = {
        "from": context.activity.from_property.name,
        "text": context.activity.text,
        "timestamp": str(context.activity.timestamp)
    }
    
    # Add to both full history and snippet
    state.conversation.all_messages.append(message_data)

    new_message_text = f"{message_data['from']}: {message_data['text']}"
    new_message_tokens = len(enc.encode(new_message_text)) if enc else len(new_message_text) // 4

    # 2. Ignore any single message that is too large to ever fit
    if new_message_tokens > MAX_HISTORY_TOKENS:
        print(f"Warning: Incoming message ({new_message_tokens} tokens) is larger than the entire limit and will be ignored.")
        # Proceed to the AI call without adding this message to the snippet
        await bot_app.ai.run(context, state)
        return True

    # 3. Make space for the new message by removing the oldest ones
    # This loop ensures there's enough room BEFORE adding the new message.
    while (state.conversation.history_snippet_tokens + new_message_tokens) > MAX_HISTORY_TOKENS and len(state.conversation.history_snippet) > 0:
        oldest_message = state.conversation.history_snippet.pop(0)
        
        oldest_message_text = f"{oldest_message['from']}: {oldest_message['text']}"
        oldest_message_tokens = len(enc.encode(oldest_message_text)) if enc else len(oldest_message_text) // 4
        
        state.conversation.history_snippet_tokens -= oldest_message_tokens

    # 4. Add the new message now that space is guaranteed
    state.conversation.history_snippet.append(message_data)
    state.conversation.history_snippet_tokens += new_message_tokens
    
    print(f"Message stored: {context.activity.text}")
    print(f"All stored messages: {state.conversation.all_messages}")
    print(f"Current history snippet: {state.conversation.history_snippet}")
    print(f"Current history snippet tokens length: {state.conversation.history_snippet_tokens}")

    # Only respond when bot is mentioned (in group chats)
    if context.activity.conversation.conversation_type != "personal":
        if not is_bot_mentioned(context):
            print("Bot not mentioned - not responding.")
            return False  # Prevent response but keep storing messages

    print("Bot mentioned or 1:1 chat. Proceeding with normal Teams AI logic.")
    await bot_app.ai.run(context, state)
    return True


@bot_app.ai.action("createTask")
async def create_task(context: ActionTurnContext[Dict[str, Any]], state: AppTurnState):
    if not state.conversation.tasks:
        state.conversation.tasks = {}
    parameters = state.conversation.planner_history[-1].content.action.parameters
    task = {"title": parameters["title"], "description": parameters["description"]}
    state.conversation.tasks[parameters["title"]] = task
    return f"task created, think about your next action"

@bot_app.ai.action("deleteTask")
async def delete_task(context: ActionTurnContext[Dict[str, Any]], state: AppTurnState):
    if not state.conversation.tasks:
        state.conversation.tasks = {}
    parameters = state.conversation.planner_history[-1].content.action.parameters
    if parameters["title"] not in state.conversation.tasks:
        return "task not found, think about your next action"
    del state.conversation.tasks[parameters["title"]]
    return f"task deleted, think about your next action"

@bot_app.ai.action("get_weather")
async def get_weather(context: ActionTurnContext[Dict[str, Any]], state: AppTurnState):
    # Ensure planner history has something
    if not state.conversation.planner_history:
        await context.send_activity("❗ No planner history found.")
        return

    # Extract parameters from last planner action
    parameters = state.conversation.planner_history[-1].content.action.parameters
    print("✅ Parameters from planner:", parameters)

    city = parameters.get("city")
    if not city:
        await context.send_activity("⚠️ Please specify a city.")
        return "City is required."

    import httpx
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://localhost:8000/weather?city={city}")
            data = response.json()
            message = f"🌤️ Weather in {city}: {data.get('temp')}, {data.get('status')}"
            await context.send_activity(message)
            return message
    except Exception as e:
        error_msg = f"❌ Error fetching weather: {e}"
        await context.send_activity(error_msg)
        return error_msg
    
@bot_app.ai.action("get_nutrition")
async def get_nutrition(context: ActionTurnContext[Dict[str, Any]], state: AppTurnState):
    if not state.conversation.planner_history:
        await context.send_activity("❗ No planner history available.")
        return msg

    # Extract the food name from planner history
    parameters = state.conversation.planner_history[-1].content.action.parameters
    food = parameters.get("food")
    if not food:
        await context.send_activity("⚠️ Please provide a food name.")
        return "Food is required."

    import httpx
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://localhost:8000/nutrition?food={food}")
            data = response.json()
            msg = (
                f"🍽️ Nutrition facts for {food}:\n"
                f"Calories: {data['calories']} kcal\n"
                f"Protein: {data['protein']} g\n"
                f"Carbs: {data['carbs']} g\n"
                f"Fat: {data['fat']} g"
            )
            await context.send_activity(msg)
            return msg
    except Exception as e:
        error_msg = f"❌ Failed to fetch nutrition data: {e}"
        await context.send_activity(error_msg)
        return error_msg
    
@bot_app.error
async def on_error(context: TurnContext, error: Exception):
    # This check writes out errors to console log .vs. app insights.
    # NOTE: In production environment, you should consider logging this to Azure
    #       application insights.
    print(f"\n [on_turn_error] unhandled error: {error}", file=sys.stderr)
    traceback.print_exc()

    # Send a message to the user
    await context.send_activity("The agent encountered an error or bug.")

@bot_app.feedback_loop()
async def feedback_loop(_context: TurnContext, _state: TurnState, feedback_loop_data: FeedbackLoopData):
    # Add custom feedback process logic here.
    print(f"Your feedback is:\n{json.dumps(asdict(feedback_loop_data), indent=4)}")