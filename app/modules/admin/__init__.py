"""The administration module: editorial writes and account moderation.

Everything a learner can *read* from the catalogue lives in ``modules/content``. What
lives here is the other half — the writes that change the catalogue — plus the audit
trail that records them. Keeping the two apart is deliberate: the public read slice must
never be able to mutate content, and the editorial slice must never accidentally become
part of the unauthenticated surface.
"""
