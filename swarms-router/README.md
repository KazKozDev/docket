# Swarms на локальном роутере

Это не самодельный оркестратор. Здесь официальный пакет [`swarms`](https://docs.swarms.world):

- `Agent`
- `HierarchicalSwarm`
- `SequentialWorkflow`
- `AutoSwarmBuilder`

Все LLM-вызовы идут в твой OpenAI-совместимый роутер:

`http://127.0.0.1:8080/v1`

Документация фреймворка по кастомному endpoint: `llm_base_url` + `llm_api_key`.

## Установка

```bash
cd swarms-router
python3 -m venv .venv
source .venv/bin/activate
pip install -U -r requirements.txt
cp .env.example .env
```

Посмотри id моделей на роутере:

```bash
python main.py --models
```

Пропиши их в `.env`:

```env
OPENAI_BASE_URL=http://127.0.0.1:8080/v1
OPENAI_API_KEY=local
ORCH_MODEL=умная-модель-с-роутера
WORKER_MODEL=локальная-или-дешёвая
```

## Запуск настоящего swarm

Иерархия (директор раздаёт воркерам) — это основной режим:

```bash
python main.py --type hierarchical "Сравни 3 подхода к multi-agent LLM и скажи, что ставить на локальный роутер"
```

Последовательный пайплайн Researcher → Analyst → Writer → Critic:

```bash
python main.py --type sequential "Та же задача"
```

Автосборка команды под задачу (`AutoSwarmBuilder`):

```bash
python main.py --type auto "Придумай и выполни план вывода нового SaaS на рынок"
```

Модели на один запуск:

```bash
python main.py --type hierarchical --orch claude-sonnet --worker qwen2.5 "задача"
```

## Что происходит внутри

`HierarchicalSwarm` создаёт директора и воркеров `Researcher / Analyst / Writer / Critic`. Директор режет задачу и гоняет её по агентам до `MAX_LOOPS`.

`AutoSwarmBuilder` сам проектирует состав агентов и topology под текст задачи, потом запускает `SwarmRouter`.

Оба класса — из пакета `swarms`, не из самописного цикла.
