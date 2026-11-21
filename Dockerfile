# Kentucky Fried Evan - Mech Royale bot (kfe2, our own Python bot)
FROM python:3.13-slim
RUN pip install --no-cache-dir numpy==2.1.3
WORKDIR /bot
COPY mechfight.py worldmap.py kfe2.py ./
ENTRYPOINT ["python3", "-u", "/bot/kfe2.py"]
