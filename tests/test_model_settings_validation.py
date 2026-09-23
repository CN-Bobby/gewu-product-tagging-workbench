import base64
import io
from PIL import Image
import pytest
from backend import model_settings


class Response:
    def __init__(self,status=200,payload=None):
        self.status_code=status;self._payload=payload or {'choices':[{'message':{'content':'OK'}}]}
    def json(self):return self._payload


class Client:
    responses=[]
    calls=[]
    def __init__(self,timeout):self.timeout=timeout
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def post(self,endpoint,headers,json):
        self.__class__.calls.append((endpoint,headers,json))
        return self.__class__.responses.pop(0)


def test_validate_models_uses_two_non_business_requests(monkeypatch):
    Client.calls=[];Client.responses=[Response(),Response(payload={'choices':[{'message':{'content':'{"order":["circle","triangle","square"]}'}}]})];monkeypatch.setattr(model_settings.httpx,'Client',Client)
    result=model_settings.validate_models('https://example.test/v1','secret-key','text','vision')
    assert result['status']=='validated'
    assert len(Client.calls)==2
    assert all(call[0]=='https://example.test/v1/chat/completions' for call in Client.calls)
    assert Client.calls[0][2]['model']=='text' and Client.calls[0][2]['max_tokens']==32
    assert Client.calls[0][2]['messages'][0]['content']=='连接测试，仅回复 OK'
    image=Client.calls[1][2]['messages'][0]['content'][1]['image_url']['url']
    assert image.startswith('data:image/jpeg;base64,')
    with Image.open(io.BytesIO(base64.b64decode(image.partition(',')[2]))) as fixture:
        assert fixture.size==(512,512) and fixture.mode=='RGB' and fixture.format=='JPEG'
    assert '商品' not in str(Client.calls[1][2])


def test_deepseek_flash_image_probe_disables_thinking_and_has_output_room(monkeypatch):
    Client.calls=[]
    Client.responses=[Response(),Response(payload={'choices':[{'message':{'content':'{"order":["circle","triangle","square"]}'}}]})]
    monkeypatch.setattr(model_settings.httpx,'Client',Client)
    result=model_settings.validate_models('https://api.deepseek.com/v1','secret-key',
                                          'deepseek-flash','deepseek-flash')
    assert result['status']=='validated'
    assert [call[2]['model'] for call in Client.calls]==['deepseek-flash','deepseek-flash']
    assert all(call[2]['thinking']=={'type':'disabled'} for call in Client.calls)
    assert Client.calls[0][2]['max_tokens']==32
    assert Client.calls[1][2]['max_tokens']>=256


def test_non_deepseek_provider_keeps_its_existing_probe_shape(monkeypatch):
    Client.calls=[];Client.responses=[Response(),Response(payload={'choices':[{'message':{'content':'{"order":["circle","triangle","square"]}'}}]})]
    monkeypatch.setattr(model_settings.httpx,'Client',Client)
    model_settings.validate_models('https://provider.example/v1','secret-key','text','vision')
    assert all('thinking' not in call[2] for call in Client.calls)
    assert Client.calls[1][2]['max_tokens']>=256


def test_deepseek_probe_error_hides_endpoint_and_asin_like_identifier(monkeypatch):
    private_asin='B0'+'12345678'
    Client.calls=[]
    Client.responses=[Response(400,{'error':{'message':f'bad request https://api.deepseek.com/?key=secret {private_asin}'}}),Response()]
    monkeypatch.setattr(model_settings.httpx,'Client',Client)
    result=model_settings.validate_models('https://api.deepseek.com','secret','deepseek-flash','deepseek-flash')
    assert 'api.deepseek.com' not in str(result)
    assert private_asin not in str(result)
    assert 'secret' not in str(result)


@pytest.mark.parametrize('status,code',[(401,'authentication_failed'),(403,'authentication_failed'),(404,'endpoint_not_found'),(500,'provider_error')])
def test_validate_models_reports_independent_safe_status(monkeypatch,status,code):
    Client.calls=[];Client.responses=[Response(status,{'error':{'message':'Bearer secret-key rejected'}}),Response(status,{'error':{'message':'Bearer secret-key rejected'}})]
    monkeypatch.setattr(model_settings.httpx,'Client',Client)
    result=model_settings.validate_models('https://example.test/v1','secret-key','text','vision')
    assert result['status']=='failed'
    assert result['text']['code']==code and result['vision']['code']==code
    assert 'secret-key' not in str(result)


def test_validate_models_keeps_text_success_when_vision_is_unsupported(monkeypatch):
    Client.calls=[];Client.responses=[Response(),Response(400,{'error':{'message':'this model does not support image input'}})]
    monkeypatch.setattr(model_settings.httpx,'Client',Client)
    result=model_settings.validate_models('https://example.test/v1','secret-key','text','vision')
    assert result['status']=='partial' and result['text']['status']=='validated'
    assert result['vision']['status']=='failed' and result['vision']['code']=='vision_unsupported'


@pytest.mark.parametrize(('message','code'),[
    ('model vision-x not found','model_not_found'),
    ('invalid image format','test_image_invalid'),
])
def test_validation_distinguishes_model_name_and_test_image(monkeypatch,message,code):
    status=404 if code=='model_not_found' else 400
    Client.calls=[];Client.responses=[Response(),Response(status,{'error':{'message':message}})]
    monkeypatch.setattr(model_settings.httpx,'Client',Client)
    result=model_settings.validate_models('https://example.test/v1','secret-key','text','vision')
    assert result['status']=='partial' and result['vision']['code']==code
