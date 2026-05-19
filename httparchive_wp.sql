-- HTTP Archive: WordPress sites with flipbook-relevant signals.
-- Dataset: httparchive.crawl.pages (monthly, ~16M sites, has `technologies` array)
-- Cost: this table is ~TB-scale; the query below targets a single month and
-- filters early. Expect a few GB scanned per run (well under $5 at $5/TB).
--
-- Replace @DATE with the latest available crawl date, e.g. 2026-04-01.

WITH wp_pages AS (
  SELECT
    page                                        AS url,
    NET.REG_DOMAIN(page)                        AS domain,
    JSON_VALUE(summary, '$.rank')               AS crux_rank,
    JSON_VALUE(custom_metrics, '$.other.lang')  AS lang,
    payload,
    technologies
  FROM `httparchive.crawl.pages`
  WHERE date = DATE('@DATE')
    AND client = 'desktop'
    AND is_root_page = TRUE
    AND EXISTS (
      SELECT 1 FROM UNNEST(technologies) t
      WHERE t.technology = 'WordPress'
    )
)
SELECT
  domain,
  url,
  crux_rank,
  lang,
  -- Flipbook-relevant signals (heuristic, not exhaustive):
  ARRAY_TO_STRING(
    ARRAY(SELECT t.technology FROM UNNEST(technologies) t
          WHERE t.technology IN (
            'WooCommerce','Elementor','Issuu','FlipHTML5',
            'Yoast SEO','WPBakery','Divi','Avada'
          )),
    '|'
  ) AS tech_signals,
  -- Content hints from the HTML body (cheap regexes):
  REGEXP_CONTAINS(LOWER(payload), r'\.pdf') AS has_pdf_link,
  REGEXP_CONTAINS(LOWER(payload), r'(catalog|catalogue|brochure|magazine|lookbook|menu)') AS has_catalog_word,
  REGEXP_CONTAINS(LOWER(payload), r'(issuu\.com|fliphtml5|flipbook|3dflipbook|dflip)') AS has_flipbook_competitor
FROM wp_pages
WHERE
  -- Keep only sites that show at least one buying signal.
  REGEXP_CONTAINS(LOWER(payload), r'\.pdf')
  OR REGEXP_CONTAINS(LOWER(payload), r'(catalog|catalogue|brochure|magazine|lookbook|menu)')
  OR REGEXP_CONTAINS(LOWER(payload), r'(issuu\.com|fliphtml5|flipbook|3dflipbook|dflip)')
ORDER BY SAFE_CAST(crux_rank AS INT64) NULLS LAST
LIMIT 100000;
