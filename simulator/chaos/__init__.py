"""Operator-facing chaos control: `simulator/chaos/cli.py` writes scenario
state into Redis; `simulator.services.common.chaos.ChaosController` (running
inside each simulated service) reads it. See `scenarios.py` for the catalog.
"""
