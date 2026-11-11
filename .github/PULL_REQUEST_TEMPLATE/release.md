## Release

Version: `vX.Y.Z` (previous: `vX.Y.Z`)
Bump rationale: patch / minor / major — because

## Changelog

### Added
### Changed
### Fixed
### Removed or deprecated
### Breaking

## Risk

- [ ] Low — patch, no interface change
- [ ] Medium — minor, additive interface change
- [ ] High — breaking change or migration required

Downstream consumers affected:
Rollback tag: `vX.Y.Z`

## Evidence

```
$ pytest -q
$ ruff check . && pyright
```

CI run:

## Gates

- [ ] Version bumped in every manifest (pyproject / package.json / chart / action.yml)
- [ ] CHANGELOG.md updated and dated
- [ ] Full CI green on the release branch
- [ ] Upgrade or migration notes written for every breaking change
- [ ] Artifacts build reproducibly (wheel, image digest, tag)
- [ ] Docs and README reflect the new behavior
- [ ] Consumers notified or bump PRs opened

## Reviewer focus

## Changes by intent

**Added**
**Modified**
**Deleted**

## Files touched

<!-- FILES-TOUCHED:START -->
_pending — the bot fills this in on push_
<!-- FILES-TOUCHED:END -->
