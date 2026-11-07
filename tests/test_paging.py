from gatekeeper import paging

VMSTAT = "nr_free_pages 40000\npswpin {swapped}\npswpout 8608206\npgmajfault {faults}\n"
MEMINFO = """MemTotal:        7041024 kB
MemFree:          162816 kB
MemAvailable:    3033088 kB
Buffers:           12345 kB
SwapTotal:       4194300 kB
SwapFree:        1143276 kB
"""


def test_disk_waits_and_swap_ins_are_counted_between_two_reads(tmp_path):
    vmstat = tmp_path / "vmstat"
    vmstat.write_text(VMSTAT.format(swapped=2477802, faults=900000))
    before = paging.counters(vmstat)
    vmstat.write_text(VMSTAT.format(swapped=2480906, faults=900812))
    counts = paging.between(before, paging.counters(vmstat))
    assert counts == {"disk_waits": 812, "swap_ins": 3104}
    assert paging.describe(counts) == "waited on the disk 812 times, 3104 pages from swap"


def test_no_counts_where_there_is_no_proc(tmp_path):
    missing = tmp_path / "nothing"
    assert paging.counters(missing) == {}
    assert paging.between({}, {"pswpin": 1, "pgmajfault": 1}) == {}
    assert paging.memory(missing) is None
    # A /proc/vmstat without the counters (an unusual kernel) counts nothing either.
    (partial := tmp_path / "vmstat").write_text("pswpin 5\n")
    assert paging.counters(partial) == {}


def test_the_hosts_memory_and_swap_in_use(tmp_path):
    (meminfo := tmp_path / "meminfo").write_text(MEMINFO)
    assert paging.memory(meminfo) == {
        "total": 6876,
        "available": 2962,
        "swap_total": 4095,
        "swap_used": 2979,
    }
