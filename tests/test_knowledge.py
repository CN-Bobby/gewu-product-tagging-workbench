import pytest

from backend import knowledge


def test_search_results_unwraps_https_and_rejects_irrelevant_items():
    body='''
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Ftankini&amp;rut=x">Tankini guide</a>
    <a class="result__snippet" href="x">A tankini has a tank-style top.</a>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fserver&amp;rut=x">Server ports</a>
    <a class="result__snippet" href="x">Configure a cloud server.</a>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=http%3A%2F%2Finsecure.example%2Ftankini&amp;rut=x">Tankini over HTTP</a>
    '''
    result=knowledge.search_results(body,'tankini')
    assert len(result)==1
    assert result[0]['url']=='https://example.com/tankini'
    assert 'tankini' in (result[0]['title']+' '+result[0]['excerpt']).casefold()


def test_private_knowledge_source_is_rejected():
    with pytest.raises(ValueError,match='本机或内网'):
        knowledge.check_url('https://127.0.0.1/')
