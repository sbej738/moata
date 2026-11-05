from app.fare import Stop, onboard_after_each_stop, split_fare


def doc_example():
    """기획서 5장 예시: 같은 곳에서 3명 출발, C 3km · B 6km · A 10km 하차, F = 13,000원."""
    A, B, C = 1, 2, 3
    stops = [Stop(A, "pickup"), Stop(B, "pickup"), Stop(C, "pickup"),
             Stop(C, "dropoff"), Stop(B, "dropoff"), Stop(A, "dropoff")]
    legs = [0, 0, 3000, 3000, 4000]
    return A, B, C, stops, legs


def test_segment_split_matches_plan_example():
    A, B, C, stops, legs = doc_example()
    r = split_fare(stops, legs, {A: 1, B: 1, C: 1}, 13000)
    assert r.final == {A: 8450, B: 3250, C: 1300}
    assert sum(r.final.values()) == 13000
    assert [round(s.cost) for s in r.segments] == [3900, 3900, 5200]
    assert [s.riders for s in r.segments] == [[A, B, C], [A, B], [A]]


def test_ride_distance_comparison_matches_plan_example():
    A, B, C, stops, legs = doc_example()
    r = split_fare(stops, legs, {A: 1, B: 1, C: 1}, 13000)
    assert r.compare["ride_distance"] == {A: 6842, B: 4105, C: 2053}


def test_seats_weight_the_split():
    # 2좌석 요청은 같이 탄 구간에서 두 몫을 낸다
    stops = [Stop(1, "pickup"), Stop(2, "pickup"), Stop(2, "dropoff"), Stop(1, "dropoff")]
    r = split_fare(stops, [0, 3000, 3000], {1: 2, 2: 1}, 6000)
    # 1구간 3,000원을 2:1, 2구간 3,000원은 요청 1 혼자
    assert r.final == {1: 5000, 2: 1000}


def test_cap_moves_excess_to_others():
    # 요청 2는 단독이면 2,000원짜리라 1,800원(10% 할인)을 넘으면 안 된다
    stops = [Stop(1, "pickup"), Stop(2, "pickup"), Stop(1, "dropoff"), Stop(2, "dropoff")]
    r = split_fare(stops, [0, 4000, 4000], {1: 1, 2: 1}, 8000,
                   solo_fares={1: 9000, 2: 2000}, est_group_fare=8000)
    assert r.final[2] == 1800
    assert r.final[1] == 6200
    assert sum(r.final.values()) == 8000
    assert any("상한" in n for n in r.notes)


def test_tolls_go_only_to_riders_on_that_leg():
    A, B, C, stops, legs = doc_example()
    tolls = [0, 0, 0, 0, 1000]  # 마지막 구간(A 혼자)에서 통행료 1,000원
    r = split_fare(stops, legs, {A: 1, B: 1, C: 1}, 13000, leg_tolls=tolls)
    assert r.final == {A: 9450, B: 3250, C: 1300}


def test_rounding_remainder_goes_to_payer():
    stops = [Stop(1, "pickup"), Stop(2, "pickup"), Stop(3, "pickup"),
             Stop(3, "dropoff"), Stop(2, "dropoff"), Stop(1, "dropoff")]
    r = split_fare(stops, [0, 0, 1000, 1000, 1000], {1: 1, 2: 1, 3: 1}, 10000)
    assert sum(r.final.values()) == 10000
    assert all(v % 10 == 0 for k, v in r.final.items() if k != r.payer)


def test_onboard_states():
    stops = [Stop(1, "pickup"), Stop(2, "pickup"), Stop(1, "dropoff"), Stop(2, "dropoff")]
    assert onboard_after_each_stop(stops) == [[1], [1, 2], [2], []]
