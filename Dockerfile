# Kentucky Fried Evan - Mech Royale bot
FROM python:3.13-slim
RUN pip install --no-cache-dir numpy==2.1.3
WORKDIR /bot
COPY mechfight.py worldmap.py bot.py ./
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
