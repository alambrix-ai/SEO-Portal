"""Test doubles.

Fakes live here rather than in the application. The product talks to real
vendor APIs and a real model; substituting either is a testing concern, and
shipping a substitute would let a misconfigured deployment serve invented
data or publish invented copy.
"""
