
from rich.console import Console

_console = Console()


def success(message: str) -> None:
    _console.print(f"[bold green]SUCCESS[/bold green] {message}")


def error(message: str) -> None:
    _console.print(f"[bold red]ERROR[/bold red] {message}")


def warning(message: str) -> None:
    _console.print(f"[bold yellow]WARNING[/bold yellow] {message}")


def info(message: str) -> None:
    _console.print(f"[cyan]INFO[/cyan] {message}")


def debug(message: str) -> None:
    _console.print(f"[dim]DEBUG[/dim] {message}")


def step(message: str) -> None:
    _console.print(f"[bold blue]STEP[/bold blue] {message}")


def skip(message: str) -> None:
    _console.print(f"[dim yellow]SKIP[/dim yellow] {message}")


def data(message: str) -> None:
    _console.print(message, markup=False)
