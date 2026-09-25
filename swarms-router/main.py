#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from router_env import BASE_URL, MAX_LOOPS, list_router_models, resolve_models


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Официальный Swarms (kyegomez/swarms) через локальный OpenAI-роутер."
    )
    parser.add_argument("task", nargs="*", help="Задача")
    parser.add_argument(
        "--type",
        choices=["hierarchical", "auto", "sequential"],
        default="hierarchical",
        help="Тип swarm из фреймворка Swarms",
    )
    parser.add_argument("--models", action="store_true", help="Список моделей роутера")
    parser.add_argument("--orch", help="Модель директора / AutoSwarmBuilder")
    parser.add_argument("--worker", help="Модель воркеров")
    parser.add_argument("--loops", type=int, default=MAX_LOOPS)
    return parser.parse_args()


def read_task(parts: list[str]) -> str:
    task = " ".join(parts).strip()
    if task:
        return task
    print("Задача (пустая строка — выход):")
    task = input("> ").strip()
    return task


def run_hierarchical(task: str, orch: str, worker: str, loops: int):
    from swarms import HierarchicalSwarm

    from agents import general_workers

    swarm = HierarchicalSwarm(
        name="Router-HierarchicalSwarm",
        description="Иерархический рой через локальный OpenAI-совместимый роутер",
        agents=general_workers(worker),
        max_loops=loops,
        director_settings={
            "model_name": orch,
            "llm_base_url": BASE_URL,
            "llm_api_key": __import__("router_env").API_KEY,
            "temperature": 0.2,
        },
        verbose=True,
    )
    return swarm.run(task=task)


def run_sequential(task: str, worker: str):
    from swarms import SequentialWorkflow

    from agents import general_workers

    workflow = SequentialWorkflow(
        name="Router-SequentialWorkflow",
        agents=general_workers(worker),
    )
    return workflow.run(task)


def run_auto(task: str, orch: str):
    from swarms import AutoSwarmBuilder

    builder = AutoSwarmBuilder(
        name="Router-AutoSwarmBuilder",
        description="Автосборка swarm под задачу через локальный роутер",
        model_name=orch,
        verbose=True,
        max_loops=1,
    )
    if hasattr(builder, "build_and_run_swarm"):
        return builder.build_and_run_swarm(task)
    return builder.run(task)


def main() -> int:
    args = parse_args()
    print(f"роутер: {BASE_URL}")
    print(f"swarm type: {args.type}")

    try:
        available = list_router_models()
    except Exception as exc:
        print(exc)
        return 1

    if available:
        print("модели:", ", ".join(available[:30]) + ("…" if len(available) > 30 else ""))
    else:
        print("GET /models пустой — задай ORCH_MODEL и WORKER_MODEL в .env")

    if args.models:
        return 0

    orch, worker = resolve_models(available, args.orch, args.worker)
    print(f"директор={orch}  воркеры={worker}")

    task = read_task(args.task)
    if not task:
        return 0

    if args.type == "hierarchical":
        result = run_hierarchical(task, orch, worker, args.loops)
    elif args.type == "sequential":
        result = run_sequential(task, worker)
    else:
        result = run_auto(task, orch)

    print("\n===== SWARM OUTPUT =====\n")
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
