"""Native beat final-sync proof. Errors invalidate capture, never weaken it."""
from django_celery_beat.schedulers import DatabaseScheduler

import coordination as gate


class DrainedScheduler(DatabaseScheduler):
    def close(self):
        # Native sync catches some SQL errors internally and retains _dirty.
        # A successful exit alone therefore cannot be used as drain evidence.
        self.sync()
        clean = not self._dirty
        super().close()
        clean = clean and not self._dirty
        state = gate.current()
        if state and state.get('admission') == 'CLOSED':
            gate.atomic(gate.ROOT / 'beat-clean.json', {
                'generation': state['generation'], 'clean': clean,
                'owner': gate.identity(),
            })
