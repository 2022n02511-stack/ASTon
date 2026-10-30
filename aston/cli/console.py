
from rich.console import Console

console = Console()

def success(message: str) -> None:
    console.print(f"[bold green]SUCCESS[/bold green] {message}")

def error(message: str) -> None:
    console.print(f"[bold red]ERROR[/bold red] {message}")

def warning(message: str) -> None:
    console.print(f"[bold yellow]WARNING[/bold yellow] {message}")

def info(message: str) -> None:
    console.print(f"[cyan]INFO[/cyan] {message}")
