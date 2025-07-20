from fastapi import FastAPI, Query
from pydantic import BaseModel

app = FastAPI()

FOOD_DB = {
    "banana": {"calories": 89, "protein": 1.1, "carbs": 22.8, "fat": 0.3},
    "chicken breast": {"calories": 165, "protein": 31, "carbs": 0, "fat": 3.6},
    "apple": {"calories": 52, "protein": 0.3, "carbs": 14, "fat": 0.2},
}

class WeatherResponse(BaseModel):
    city: str
    temp: str
    status: str

@app.get("/weather", response_model=WeatherResponse)
def get_weather(city: str):
    return {"city": city, "temp": "30°C", "status": "Sunny"}

class NutritionResponse(BaseModel):
    calories: int
    protein: float
    carbs: float
    fat: float


@app.get("/nutrition", response_model=NutritionResponse)
def get_nutrtition(food: str = Query(..., description="The food name to get nutrition info for")):
    food_data = FOOD_DB.get(food.lower())
    if food not in FOOD_DB:
        return {"calories": 0, "protein": 0, "carbs": 0, "fat": 0}
    return food_data

