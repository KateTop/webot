from unittest.mock import MagicMock, patch
import pytest
from src.wechat.wcdb_client import WcdbNativeClient

def test_init_keeps_dependency_path_and_reports_both_statuses():
    client=WcdbNativeClient.__new__(WcdbNativeClient)
    client._dll_dir="synthetic-native-directory"
    client._dll_search_cookie=None
    dll=MagicMock()
    dll._handle=123
    dll.InitProtection.return_value=-101
    dll.wcdb_init.return_value=-1000
    cookie=object()
    with patch("src.wechat.wcdb_client.os.add_dll_directory",return_value=cookie) as search, \
         patch("src.wechat.wcdb_client.ct.CDLL",return_value=dll), \
         patch("src.wechat.wcdb_client._apply_drm_patch"):
        with pytest.raises(RuntimeError) as error: client.init()
        assert "wcdb_init=-1000" in str(error.value)
        assert "InitProtection=-101" in str(error.value)
        assert "尚未打开微信数据库" in str(error.value)
        assert client._dll_search_cookie is cookie
        dll.wcdb_init.return_value=0
        client.init()
        assert search.call_count==1

def test_expiry_detection_is_limited_to_verified_binary(tmp_path):
    from src.wechat.wcdb_client import _component_expiry_error
    binary=tmp_path/"wcdb_api.dll"
    binary.write_bytes(b"synthetic")
    assert _component_expiry_error(-101,str(binary)) is None
    with patch("src.wechat.wcdb_client.hashlib.sha256") as checksum:
        checksum.return_value.hexdigest.return_value="6915913a3a9930694e5c821bf58841e17bbad0f56691e307674fb0e31e9d44b8"
        assert "2026-10-01 07:59:59" in _component_expiry_error(-101,str(binary))
        assert _component_expiry_error(-1000,str(binary)) is None

def test_expired_component_stops_before_engine_initialization():
    client=WcdbNativeClient.__new__(WcdbNativeClient)
    client._dll_dir="synthetic-native-directory"
    client._dll_search_cookie=None
    dll=MagicMock();dll._handle=123;dll.InitProtection.return_value=-101
    with patch("src.wechat.wcdb_client.os.add_dll_directory"), \
         patch("src.wechat.wcdb_client.ct.CDLL",return_value=dll), \
         patch("src.wechat.wcdb_client._apply_drm_patch"), \
         patch("src.wechat.wcdb_client._component_expiry_error",return_value="组件已过期"):
        with pytest.raises(RuntimeError,match="组件已过期"):client.init()
    dll.wcdb_init.assert_not_called()
