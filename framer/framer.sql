-- Framer sites from HTTP Archive. Cheap variant: scans only the
-- `technologies` column (~66 GB), fits in the BigQuery free tier.
-- Captures BOTH desktop and mobile crawls, deduped to one row per
-- registered domain (desktop row preferred for a stable CrUX rank).
-- Replace @DATE with the latest crawl date, e.g. 2026-05-01.

SELECT
  NET.REG_DOMAIN(page) AS domain,
  page AS url,
  JSON_VALUE(summary, '$.rank') AS crux_rank,
  ARRAY_TO_STRING(
    ARRAY(SELECT t.technology FROM UNNEST(technologies) t
          WHERE t.technology IN ('Framer Sites','Issuu','FlipHTML5')),
    '|'
  ) AS tech_signals,
  EXISTS(SELECT 1 FROM UNNEST(technologies) t WHERE t.technology = 'Issuu') AS uses_issuu,
  EXISTS(SELECT 1 FROM UNNEST(technologies) t WHERE t.technology = 'FlipHTML5') AS uses_fliphtml5
FROM `httparchive.crawl.pages`
WHERE date = DATE('@DATE')
  AND is_root_page = TRUE
  AND EXISTS (
    SELECT 1 FROM UNNEST(technologies) t
    WHERE t.technology = 'Framer Sites'
  )
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY NET.REG_DOMAIN(page) ORDER BY client
) = 1
ORDER BY SAFE_CAST(JSON_VALUE(summary, '$.rank') AS INT64) NULLS LAST;
