"""Bounded startup signal timing, without recording audio or identifying speech."""
import math
import struct


class StartupAudioProbe:
    """Compare captured and successfully submitted audio for the first 30 seconds.

    RMS activity can be noise as well as speech. This diagnostic never controls
    turn detection, buffers PCM, or changes the audio sent to the model.
    """

    def __init__(self):
        self.events = []
        self.sources = {}
        self.dropped_before_ready = None

    def provider_ready(self, overflow_frames, trimmed_frames):
        self.dropped_before_ready = {
            "overflow_frames": overflow_frames, "trimmed_frames": trimmed_frames,
        }

    def observe(self, source, pcm, rate, elapsed_ms):
        if elapsed_ms > 30000 or len(self.events) >= 32 or not pcm or len(pcm) % 2:
            return []
        state = self.sources.setdefault(source, {
            "active": False, "loud_ms": 0, "quiet_ms": 0,
            "peak_rms": 0, "above_threshold_ms": 0,
        })
        count = len(pcm) // 2
        rms = math.isqrt(sum(sample[0] ** 2 for sample in struct.iter_unpack('<h', pcm)) // count)
        duration = count * 1000 / rate
        state['peak_rms'] = max(state['peak_rms'], rms)
        event = None
        if rms >= 400:
            state['above_threshold_ms'] += duration
            state['quiet_ms'] = 0
            if not state['active']:
                if not state['loud_ms']:
                    state['candidate_ms'] = elapsed_ms
                state['loud_ms'] += duration
                if state['loud_ms'] >= 60:
                    state['active'] = True
                    event = {'source': source, 'event': 'activity_start', 'elapsed_ms': state['candidate_ms']}
        else:
            state['loud_ms'] = 0
            if state['active']:
                if not state['quiet_ms']:
                    state['candidate_ms'] = elapsed_ms
                state['quiet_ms'] += duration
                if state['quiet_ms'] >= 500:
                    state['active'] = False
                    event = {'source': source, 'event': 'activity_end', 'elapsed_ms': state['candidate_ms']}
        if event:
            self.events.append(event)
            return [event]
        return []

    def snapshot(self):
        return {
            'method': 'rms_activity_not_speech_detection', 'threshold_rms': 400,
            'window_ms': 30000, 'events': [dict(event) for event in self.events],
            'sources': {name: {key: round(state[key]) for key in ('peak_rms', 'above_threshold_ms')}
                        for name, state in self.sources.items()},
            'dropped_before_provider_ready': self.dropped_before_ready,
        }
