from __future__ import annotations

from swarms import Agent

from router_env import API_KEY, BASE_URL


def make_agent(
    name: str,
    prompt: str,
    model_name: str,
    description: str = "",
    max_loops: int = 1,
) -> Agent:
    return Agent(
        agent_name=name,
        agent_description=description or name,
        system_prompt=prompt,
        model_name=model_name,
        llm_base_url=BASE_URL,
        llm_api_key=API_KEY,
        max_loops=max_loops,
        streaming_on=False,
        verbose=True,
    )


def general_workers(worker_model: str) -> list[Agent]:
    return [
        make_agent(
            "Researcher",
            "Ты исследователь. Собираешь факты, отделяешь известное от догадок, указываешь пробелы.",
            worker_model,
            "Исследование и сбор фактов",
        ),
        make_agent(
            "Analyst",
            "Ты аналитик. Сравниваешь варианты, считаешь следствия, формулируешь вывод с рисками.",
            worker_model,
            "Анализ и сравнение",
        ),
        make_agent(
            "Writer",
            "Ты писатель-сборщик. Делаешь финальный текст по материалам других агентов. Ничего не выдумываешь.",
            worker_model,
            "Сборка ответа",
        ),
        make_agent(
            "Critic",
            "Ты критик. Ищешь дыры, выдумки и невыполненные критерии. Пишешь, что исправить.",
            worker_model,
            "Проверка качества",
        ),
    ]
