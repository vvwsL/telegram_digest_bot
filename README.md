# Telegram дайджест-бот (минимум токенов)

Бот собирает сообщения из выбранных источников по темам только внутри заданных окон и раз в окно отправляет дайджест по каждой теме.

## Быстрый старт (Docker)
1. Скопируйте `.env.example` в `.env` и заполните значения.
2. В `ADMIN_IDS` укажите свой Telegram user id (можно несколько через запятую).
3. Запустите:

```bash
docker compose up --build -d
```

## Запуск без Docker
1. Установите зависимости:

```bash
pip install -r requirements.txt
```

2. Экспортируйте переменные окружения и запустите:

```bash
python -m app.main
```

## Требования к источникам
- Бот должен быть добавлен в канал и иметь права, чтобы получать посты.
- В группах нужно отключить privacy mode у бота, иначе он не видит сообщения.

## Команды (админ)
- `/topic_add <name>`
- `/topic_rename <old> <new>`
- `/topic_remove <name>`
- `/topic_list`
- `/topic_src_add <topic> @channel` или ответом на forward из канала
- `/topic_src_remove <topic> @channel` или ответом на forward
- `/topic_kw_add <topic> <keyword>`
- `/topic_kw_remove <topic> <keyword>`
- `/topic_kw_list <topic>`
- `/window_add <days> <HH:MM-HH:MM>`
- `/window_remove <id>`
- `/window_list`
- `/setoutput`
- `/status`

## Примеры
Добавить тему и источник:

```
/topic_add ИИ
/topic_src_add ИИ @ai_newz
```

Добавить окно:

```
/window_add пн-пт 00:00-08:00
```

Установить чат для дайджестов:

```
/setoutput
```

## Формат дней для окон
- `пн,вт,ср` или `mon,tue,wed`
- Диапазон: `пн-пт` или `mon-fri`
- Все дни: `all` или `*`
