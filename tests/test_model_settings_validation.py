import pytest
from backend import model_settings


class Response:
    def __init__(self,status=200,payload=None):
        self.status_code=status;self._payload=payload or {'choices':[{'message':{'content':'OK'}}]}
    def json(self):return self._payload


class Client:
    response=Response()
    calls=[]
    def __init__(self,timeout):self.timeout=timeout
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def post(self,endpoint,headers,json):
        self.__class__.calls.append((endpoint,headers,json));return self.__class__.response


def test_validate_models_uses_two_non_business_requests(monkeypatch):
    Client.calls=[];Client.response=Response();monkeypatch.setattr(model_settings.httpx,'Client',Client)
    result=model_settings.validate_models('https://example.test/v1','secret-key','text','vision')
    assert result['status']=='validated'
    assert len(Client.calls)==2
    assert all(call[0]=='https://example.test/v1/chat/completions' for call in Client.calls)
    assert Client.calls[0][2]['model']=='text' and Client.calls[0][2]['max_tokens']==1
    assert Client.calls[0][2]['messages'][0]['content']=='连接测试，仅回复 OK'
    image=Client.calls[1][2]['messages'][0]['content'][1]['image_url']['url']
    assert image.startswith('data:image/png;base64,')
    assert '商品' not in str(Client.calls[1][2])


@pytest.mark.parametrize('status,expected',[(401,'授权无效'),(403,'授权无效'),(404,'模型或接口不存在'),(500,'HTTP 500')])
def test_validate_models_reports_safe_http_reason(monkeypatch,status,expected):
    Client.calls=[];Client.response=Response(status,{'error':{'message':'Bearer secret-key rejected'}})
    monkeypatch.setattr(model_settings.httpx,'Client',Client)
    with pytest.raises(ValueError,match=expected):model_settings.validate_models('https://example.test/v1','secret-key','text','vision')
    # no response or diagnostic should echo the credential
    try:model_settings.validate_models('https://example.test/v1','secret-key','text','vision')
    except ValueError as exc:assert 'secret-key' not in str(exc)
