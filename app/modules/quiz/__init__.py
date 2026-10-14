"""The attempt engine: start a lesson session, grade answers, record the result.

This module owns the one thing the catalogue deliberately cannot do — decide whether an
answer is right. The answer key stays on the server; the client posts an answer and is
told the verdict, the correct answer and the explanation. See ``domain/policy.py`` for the
rule and ``application/quiz_service.py`` for the lifecycle.
"""
