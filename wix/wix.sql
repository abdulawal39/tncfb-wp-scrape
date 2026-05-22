-- Wix sites from HTTP Archive. Cheap variant: scans only the
-- `technologies` column (~5-20 GB), fits in the BigQuery free tier.
-- Replace @DATE with the latest crawl date, e.g. 2026-05-01.

SELECT
  NET.REG_DOMAIN(page) AS domain,
  page AS url,
  JSON_VALUE(summary, '$.rank') AS crux_rank,
  ARRAY_TO_STRING(
    ARRAY(SELECT t.technology FROM UNNEST(technologies) t
          WHERE t.technology IN ('Wix','Issuu','FlipHTML5')),
    '|'
  ) AS tech_signals,
  EXISTS(SELECT 1 FROM UNNEST(technologies) t WHERE t.technology = 'Issuu') AS uses_issuu,
  EXISTS(SELECT 1 FROM UNNEST(technologies) t WHERE t.technology = 'FlipHTML5') AS uses_fliphtml5
FROM `httparchive.crawl.pages`
WHERE date = DATE('@DATE')
  AND client = 'desktop'
  AND is_root_page = TRUE
  AND EXISTS (
    SELECT 1 FROM UNNEST(technologies) t
    WHERE t.technology = 'Wix'
  )
ORDER BY SAFE_CAST(JSON_VALUE(summary, '$.rank') AS INT64) NULLS LAST;
