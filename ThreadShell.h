#pragma once
#include <thread>
#include <atomic>
#include <functional>
#include <memory>
#include <iostream>
#include <string>

/**
 * @class CThreadShell
 * @brief A wrapper for background tasks that can be stored in AnyValue.
 * AnyValue에 저장 가능한 배경 작업 래퍼입니다.
 */
class CThreadShell {
public:
    using TaskFunc = std::function<void(std::atomic<bool>& stopFlag)>;

    CThreadShell() : m_isRunning(false) {
        m_stopFlag = std::make_shared<std::atomic<bool>>(false);
    }

    // Since AnyValue requires copyability for its clone mechanism, 
    // we use shared_ptr to share the thread handle and stop flag.
    // AnyValue의 복제 메커니즘을 위해 shared_ptr로 쓰레드 핸들과 정지 플래그를 공유합니다.

    /**
     * @brief Starts a background task.
     * 배경 작업을 시작합니다.
     */
    void launch(const std::string& name, TaskFunc func) {
        if (m_isRunning) return;
        
        m_name = name;
        *m_stopFlag = false;
        m_thread = std::make_shared<std::thread>([this, func]() {
            std::cout << "[ThreadShell] '" << m_name << "' started.\n";
            func(*m_stopFlag);
            std::cout << "[ThreadShell] '" << m_name << "' finished.\n";
        });
        m_isRunning = true;
    }

    /**
     * @brief Requests the thread to stop.
     * 쓰레드에 정지를 요청합니다.
     */
    void stop() {
        if (m_stopFlag) *m_stopFlag = true;
        if (m_thread && m_thread->joinable()) {
            m_thread->detach(); // Detach for simplicity in this shell
        }
        m_isRunning = false;
    }

    bool isRunning() const { return m_isRunning; }
    std::string getName() const { return m_name; }

private:
    std::string m_name;
    bool m_isRunning = false;
    std::shared_ptr<std::atomic<bool>> m_stopFlag;
    std::shared_ptr<std::thread> m_thread;
};