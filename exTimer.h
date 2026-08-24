#pragma once

#include <functional>
#include <thread>
#include <chrono>
#include <atomic>
#include <mutex>
#include <memory>
#include <condition_variable>
#include <vector>
#include <iostream>

namespace nsExTimer {

/**
 * @class stopwatch
 * @brief Platform-independent high-resolution stopwatch.
 * 플랫폼 독립적인 고해상도 스톱워치입니다.
 */
class stopwatch {
private:
    std::chrono::high_resolution_clock::time_point _start, _end;
    std::chrono::milliseconds _elapsed{ 0 };
public:
    stopwatch(bool startNow = false) {
        if (startNow) start();
    }

    void start() {
        _start = std::chrono::high_resolution_clock::now();
    }

    uint32_t stop() {
        _end = std::chrono::high_resolution_clock::now();
        _elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(_end - _start);
        return static_cast<uint32_t>(_elapsed.count());
    }

    std::chrono::milliseconds restOf(std::chrono::milliseconds duration) {
        _end = std::chrono::high_resolution_clock::now();
        _elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(_end - _start);
        auto rest = duration - _elapsed;
        return (rest < std::chrono::milliseconds(0)) ? std::chrono::milliseconds(0) : rest;
    }
};

/**
 * @brief Portable single shot task execution.
 * 이식 가능한 단발성 작업 실행 함수입니다.
 */
static void singleShot(std::function<void()> task, uint32_t delay_ms = 0) {
    std::thread([delay_ms, task = std::move(task)]() {
        if (delay_ms > 0) {
            std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
        }
        task();
    }).detach();
}

} // namespace nsExTimer

/**
 * @class exTimer
 * @brief Pure C++ cross-platform timer with periodic and single-shot support.
 * 주기적 및 단발성 실행을 지원하는 순수 C++ 크로스 플랫폼 타이머입니다.
 */
class exTimer : public std::enable_shared_from_this<exTimer> {
public:
    explicit exTimer(std::function<void()> task, uint32_t interval_ms)
        : _task(std::move(task)), _interval(std::chrono::milliseconds(interval_ms)) {}

    ~exTimer() { stop(); }

    // Disable copy
    exTimer(const exTimer&) = delete;
    exTimer& operator=(const exTimer&) = delete;

    void start(uint32_t delay_ms = 0) {
        std::lock_guard<std::mutex> lock(_mtx);
        _singleShotMode = false;
        _start_nolock(delay_ms);
    }

    void singleShot(uint32_t delay_ms = 0) {
        std::lock_guard<std::mutex> lock(_mtx);
        _singleShotMode = true;
        _paused = false;
        if (!_running) {
            _start_nolock(delay_ms);  // Fix(A): _mtx 이미 보유 중이므로 start() 대신 _start_nolock() 호출
        } else {
            _cv.notify_one();
        }
    }

    void stop() {
        {
            std::lock_guard<std::mutex> lock(_mtx);
            // Fix(B): 조기 반환 제거 — singleShot 자연 종료 시 _running이 이미 false여도
            // _thread가 joinable 상태일 수 있으므로 항상 join까지 진행해야 std::terminate 방지
            _running = false;
            _paused = true;
        }
        _cv.notify_all();
        if (_thread && _thread->joinable()) {
            _thread->join();
        }
        _thread.reset();
    }

    bool isRunning() const { return _running; }

private:
    // Fix(A): start()의 실제 로직. 호출자가 이미 _mtx를 보유 중일 때 사용.
    void _start_nolock(uint32_t delay_ms) {
        if (_running) return;
        _running = true;
        _paused = false;
        _thread = std::make_unique<std::thread>([this, delay_ms]() {
            if (delay_ms > 0)
                std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
            _runLoop();
        });
    }

    void _runLoop() {
        while (_running) {
            {
                std::unique_lock<std::mutex> lock(_mtx);
                if (_paused && !_singleShotMode) {
                    _cv.wait(lock, [this] { return !_paused || !_running || _singleShotMode; });
                }
                if (!_running) break;
            }

            nsExTimer::stopwatch sw(true);
            try {
                if (_task) _task();
            } catch (...) {
                // Potential for error callback here
            }

            if (_singleShotMode) {
                _running = false;
                break;
            }

            // Sleep for the remaining interval
            auto rest = sw.restOf(_interval);
            if (rest.count() > 0) {
                std::unique_lock<std::mutex> lock(_mtx);
                _cv.wait_for(lock, rest, [this] { return !_running || _paused; });
            }
        }
    }

    std::function<void()> _task;
    std::chrono::milliseconds _interval;
    std::unique_ptr<std::thread> _thread;
    std::mutex _mtx;
    std::condition_variable _cv;
    std::atomic<bool> _running{false};
    std::atomic<bool> _paused{true};
    std::atomic<bool> _singleShotMode{false};
};