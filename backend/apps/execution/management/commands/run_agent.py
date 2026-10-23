from django.core.management.base import BaseCommand, CommandError

from apps.execution.runner import AgentRunner, RunnerLockError


class Command(BaseCommand):
    help = "Run the trading agent loop (single instance, protected by a PostgreSQL advisory lock)."

    def add_arguments(self, parser):
        parser.add_argument("--cycles", type=int, default=None, help="stop after N cycles (default: run forever)")

    def handle(self, *args, cycles=None, **options):
        runner = AgentRunner()
        self.stdout.write(f"agent runner {runner.name} using gateway '{runner.gateway.name}'")
        if runner.gateway.name == "mock":
            self.stdout.write(self.style.WARNING("MOCK gateway: simulated terminal, NOT a real MT5 connection"))
        try:
            runner.run(max_cycles=cycles)
        except RunnerLockError as exc:
            raise CommandError(str(exc)) from exc
        except KeyboardInterrupt:
            self.stdout.write("stopped")
