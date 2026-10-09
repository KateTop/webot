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
