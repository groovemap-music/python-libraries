# History-preserving extraction

Historical source repository: the immutable Git source assigned to
`SOURCE_REPOSITORY` below.

Source branch: `main`

Source head at extraction: `204f49e2429f074546dfc67e6354be2529a983ac`

The destination was prepared in a new local clone. The original monorepo was never
rewritten or used as the filter-repo working directory.

```bash
readonly SOURCE_REPOSITORY='https://github.com/SimplicityGuy/discogsography.git'
readonly SOURCE_CHECKOUT='../groovemap-source'
readonly DESTINATION_CHECKOUT='../python-libraries'

git clone --no-local --single-branch --branch main \
  "${SOURCE_REPOSITORY}" "${SOURCE_CHECKOUT}"

git clone --no-local --single-branch --branch main \
  "${SOURCE_CHECKOUT}" "${DESTINATION_CHECKOUT}"

git -C "${DESTINATION_CHECKOUT}" filter-repo --force \
  --path common/ \
  --path tests/common/ \
  --path tests/test_health_server.py \
  --path LICENSE \
  --path-rename common/: \
  --path-rename tests/common/:tests/
```

The filtered history contains 154 commits. Migration-era package separation was then
promoted into the destination by commit
`28fa329702bc76896cc54ab8d05ec5b1bd3d929e`. Obsolete
Python extraction-state and OAuth modules were removed from the current tree because their
ownership moved to `catalog-ingestion` and `catalog-api`; their earlier revisions remain in
this repository's filtered history.
