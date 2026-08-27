# History-preserving extraction

Source repository: `SimplicityGuy/discogsography`

Source branch: `main`

Source head at extraction: `204f49e2429f074546dfc67e6354be2529a983ac`

The destination was prepared in a new local clone. The original monorepo was never
rewritten or used as the filter-repo working directory.

```bash
git clone --no-local --single-branch --branch main \
  /Users/Robert/workspaces/github/SimplicityGuy/discogsography \
  /Users/Robert/workspaces/github/groovemap/python-libraries

git filter-repo --force \
  --path common/ \
  --path tests/common/ \
  --path tests/test_health_server.py \
  --path LICENSE \
  --path-rename common/: \
  --path-rename tests/common/:tests/
```

The filtered history contains 154 commits. Migration-era package separation from bead
`discogsography-2kpm.3` was then promoted into the destination as a new commit. Obsolete
Python extraction-state and OAuth modules were removed from the current tree because their
ownership moved to `catalog-ingestion` and `catalog-api`; their earlier revisions remain in
this repository's filtered history.
