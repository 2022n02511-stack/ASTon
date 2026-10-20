from rich.console import Console
from rich.panel import Panel
from rich.text import Text
import time
from rich.spinner import Spinner
from rich.live import Live

console = Console()

from colorama import init, Fore, Style

# Initialize colorama for cross-platform color support
init(autoreset=True)

def print_aston_banner():
    """
    Renders the block banner using TrueColor RGB escape codes 
    so it forces exact peach and green shades on any terminal theme.

    """
    print()
    # print()

    banner_lines = [
        " █████╗ ███████╗████████╗ ██████╗ ███╗   ██╗                        ",
        "██╔══██╗██╔════╗╚══██╔══╝██╔═══██╗████╗  ██║                        ",
        "███████║███████║   ██║   ██║   ██║██╔██╗ ██║                        ",
        "██╔══██║╚════██║   ██║   ██║   ██║██║╚██╗██║                        ",
        "██║  ██║███████║   ██║   ╚██████╔╝██║ ╚████║                        ",
        "╚═╝  ╚═╝╚══════╝   ╚═╝    ╚═════╝ ╚═╝  ╚═══╝                        ",
        "                                                                   ",
        "  ██████╗██╗     ██╗                     ",
        " ██╔════╝██║     ██║                   ",
        " ██║     ██║     ██║                    ",
        " ██║     ██║     ██║                       ",
        " ╚██████╗███████╗██║                       ",
        "  ╚═════╝╚══════╝╚═╝                 "
    ]

    # 24-bit TrueColor RGB codes: \033[38;2;R;G;Bm
    GREEN = "\033[38;2;46;204;113m"
    WHITE = "\033[38;2;240;240;240m"
    CYAN = "\033[38;2;0;210;255m"
    DIM_GREEN = "\033[38;2;30;130;70m"
    RESET = "\033[0m"

    for line in banner_lines:
        print(f"{GREEN}{line:<57}{RESET}")

    # for line in bird_lines:
    #     print(f" {GREEN}{line:<57}{RESET} ")
 

    print()
    # print()

    # print(f"\n{GREEN}{banner}{RESET}")
    print(f"{GREEN}─────────────────────────────────────────────────────────────────{RESET}")
    print(f"{GREEN} ASTON v1.0.1 {RESET} | {CYAN}Multi-Module Migration Pipeline{RESET} | {GREEN}System Ready.{RESET}")
    print(f"{GREEN}─────────────────────────────────────────────────────────────────{RESET}\n")

if __name__ == "__main__":
    print_aston_banner()

def print_rich_aston_banner():
    # Create styled text
    title = Text("ASTON", style="bold cyan", justify="center")
    subtitle = Text("Multi-Module Python Dependency Migration Engine", style="italic yellow", justify="center")
    
    # Combine into a sleek panel
    panel = Panel(
        Text.assemble(title, "\n", subtitle),
        border_style="bright_blue",
        padding=(1, 4),
        title="[bold green]STATUS: INITIALIZING[/bold green]",
        title_align="left"
    )
    
    console.print(panel)


def run_with_animation(description: str, task_func, *args, **kwargs):
    """
    Runs any blocking task (like scraping, validation, or docker checks)
    with an active, animated terminal spinner.
    """
    # Choose styles: "dots", "line", "circleFrames", "aesthetic", "growVertical"
    spinner = Spinner("dots", text=f"[bold cyan]{description}[/bold cyan]", style="green")
    
    with Live(spinner, console=console, refresh_per_second=12):
        # Execute your actual function while the spinner animates live
        result = task_func(*args, **kwargs)
        
    return result