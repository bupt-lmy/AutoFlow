"""AxonFlow CLI 主入口"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from axonflow import __version__
from axonflow.cli.commands.agent import agent_app

app = typer.Typer(
    name="axonflow",
    help="AxonFlow — 基于多智能体的自治工作流引擎",
    no_args_is_help=True,
)
console = Console()


def _get_engine(config_dir: str = "config"):
    """Create a CLI engine with the same encrypted credential store as the API."""
    from axonflow.config.loader import load_global_config
    from axonflow.engine import AxonFlowEngine
    from axonflow.platform.store import PlatformStore

    config_path = Path(config_dir)
    config = load_global_config(config_path / "axonflow.yaml")
    workspace_dir = Path(config.workspace_dir)
    if not workspace_dir.is_absolute():
        workspace_dir = config_path.parent / workspace_dir
    platform_store = PlatformStore(workspace_dir / "axonflow.db")
    return (
        AxonFlowEngine(
            config_dir=config_dir,
            config=config,
            platform_store=platform_store,
        ),
        platform_store,
    )


@app.command()
def start(
    daemon: bool = typer.Option(False, "--daemon", "-d", help="以守护进程模式运行"),
    config_dir: str = typer.Option("config", "--config", "-c", help="配置目录路径"),
) -> None:
    """启动 AxonFlow 引擎"""
    engine, platform_store = _get_engine(config_dir)

    console.print(f"[bold green]AxonFlow v{__version__}[/bold green]")
    console.print("Starting engine...")

    async def _run():
        await engine.start()
        console.print("[green]Engine started. Press Ctrl+C to stop.[/green]")
        try:
            # 保持运行
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await engine.stop()
            platform_store.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")


@app.command()
def run(
    workflow: str = typer.Argument(help="工作流 ID"),
    input_data: str = typer.Option("", "--input", "-i", help="输入数据 / 需求描述"),
    config_dir: str = typer.Option("config", "--config", "-c", help="配置目录路径"),
) -> None:
    """执行指定工作流"""
    engine, platform_store = _get_engine(config_dir)

    async def _run():
        try:
            await engine.initialize()

            # 启动 Agent 监听
            await engine.start()

            # 执行工作流
            console.print(f"Running workflow: [bold]{workflow}[/bold]")
            result = await engine.run_workflow(workflow, input_data)

            # 输出结果
            console.print("\n[bold]Workflow Result:[/bold]")
            console.print_json(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        finally:
            await engine.stop()
            platform_store.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1) from e


@app.command()
def status(
    config_dir: str = typer.Option("config", "--config", "-c", help="配置目录路径"),
) -> None:
    """查看系统状态"""
    from pathlib import Path

    from axonflow.config.loader import load_all_agent_configs, load_all_workflow_configs

    config_path = Path(config_dir)

    # Agent 信息
    agents = load_all_agent_configs(config_path / "agents")
    table = Table(title="Registered Agents")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Model", style="yellow")
    table.add_column("Tools", style="magenta")

    for a in agents:
        table.add_row(
            a.id,
            a.name,
            f"{a.model.provider}/{a.model.name}",
            ", ".join(a.tools) or "-",
        )

    console.print(table)

    # 工作流信息
    workflows = load_all_workflow_configs(config_path / "workflows")
    wf_table = Table(title="Registered Workflows")
    wf_table.add_column("ID", style="cyan")
    wf_table.add_column("Name", style="green")
    wf_table.add_column("Trigger", style="yellow")
    wf_table.add_column("Agents", style="magenta")

    for wf in workflows:
        trigger = wf.trigger.type
        if wf.trigger.cron:
            trigger += f" ({wf.trigger.cron})"
        wf_table.add_row(
            wf.id,
            wf.name,
            trigger,
            ", ".join(wf.agents),
        )

    console.print(wf_table)


@app.command()
def version() -> None:
    """显示版本信息"""
    console.print(f"AxonFlow v{__version__}")


app.add_typer(agent_app, name="agent", help="智能体管理")


def main() -> None:
    """CLI 入口点"""
    app()
