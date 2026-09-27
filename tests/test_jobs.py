import jobs


def test_slug_from_url_beats_guessing():
    """Где есть ссылка, угадывать нечего — и ошибиться нельзя."""
    assert jobs.from_url("https://jobs.ashbyhq.com/constructor") == ("ashby", "constructor")
    assert jobs.from_url("https://job-boards.greenhouse.io/nebius") == ("greenhouse", "nebius")
    assert jobs.from_url("https://jobs.lever.co/appfollow") == ("lever", "appfollow")
    assert jobs.from_url("https://praktika.teamtailor.com/jobs") == ("teamtailor", "praktika")
    assert jobs.from_url("https://elixi.com/careers") is None


def test_slug_variants_skip_short_first_word():
    """«ABC Fitness» под slug abc вёл на чужую доску ThoughtWorks."""
    assert jobs.slug_variants("Salmon Group") == ["salmongroup", "salmon-group", "salmon"]
    assert jobs.slug_variants("ABC Fitness") == ["abcfitness", "abc-fitness"]
    assert jobs.slug_variants("Plata") == ["plata"]
    assert jobs.slug_variants("---") == []


def test_same_company_tolerates_punctuation_and_suffix():
    assert jobs.same_company("ASOS.com", "ASOS")
    assert jobs.same_company("Salmon", "Salmon Group")
    assert not jobs.same_company("ABC Fitness", "ThoughtWorks_new")
    assert not jobs.same_company("", "Acme")


def test_posted_date_normalised_from_every_shape():
    assert jobs.posted("2026-09-21T08:00:59.084+00:00") == "2026-09-21"
    assert jobs.posted(1788965250140) == "2026-09-09"      # Lever, миллисекунды
    assert jobs.posted("2026-09-15 16:47:19 UTC") == "2026-09-15"   # Recruitee
    assert jobs.posted(None) is None and jobs.posted("") is None


def test_embedded_payload_parsed():
    """top.co держит вакансии в payload Next.js, а не в API."""
    html = (r'\"id\":\"9699\",\"position\":\"Director of Risk\",\"location\":\"x\",'
            r'\"company\":{\"name\":\"Wallet\"}')
    assert jobs.JOB_IN_PAYLOAD.findall(html) == [("9699", "Director of Risk", "Wallet")]


def test_every_adapter_is_registered():
    for name in ("ashby", "greenhouse", "lever", "smartrecruiters", "workable",
                 "recruitee", "teamtailor", "pinpoint"):
        assert callable(jobs.ADAPTERS[name])
