class Limiter:
    def __init__(self, *a, **k): pass
    def limit(self, *a, **k):
        return lambda f: f
    def exempt(self, f): return f
