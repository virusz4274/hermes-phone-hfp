"""Hermes platform plugin for Bluetooth HFP phone calls."""

from .adapter import HFPPhoneAdapter, check_requirements, register

__all__ = ["HFPPhoneAdapter", "check_requirements", "register"]
