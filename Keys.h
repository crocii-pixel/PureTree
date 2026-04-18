#pragma once
#include "AnyValue.h"
#include "CPublisher.h"
#include <string>
#include <map>
#include <mutex>

class Keys {
public:
    using onEventAction = std::function<void(const std::string&, const AnyValue&)>;

    static Keys& self() { static Keys instance; return instance; }

    void addEvent(Account& acc, const std::string& key, onEventAction func, bool allowLoopback = false) {
        if (!func || key.empty()) return;
        std::lock_guard<std::recursive_mutex> lock(m_mutex);
        m_publisher.subscribe(acc, key, [func, key](const AnyValue& v) { func(key, v); }, allowLoopback);
    }

    void raiseEvent(const std::string& key, const AnyValue& value, void* originator = nullptr) {
        { std::lock_guard<std::recursive_mutex> lock(m_mutex); m_stateStore.setValue(key, value); }
        m_publisher.publish(key, value, originator);
    }

    AnyValue getValue(const std::string& key) { std::lock_guard<std::recursive_mutex> lock(m_mutex); return m_stateStore.getValue(key); }

private:
    Keys() = default;
    ~Keys() = default;
    Keys(const Keys&) = delete;

    class XYs {
    public:
        AnyValue getValue(const std::string& k) { std::lock_guard<std::mutex> l(m_m); auto it = m_s.find(k); return (it != m_s.end()) ? it->second : AnyValue(); }
        void setValue(const std::string& k, const AnyValue& v) { std::lock_guard<std::mutex> l(m_m); m_s[k] = v; }
    private:
        std::map<std::string, AnyValue> m_s;
        std::mutex m_m;
    } m_stateStore;

    CPublisher<std::string, AnyValue> m_publisher;
    std::recursive_mutex m_mutex;
};

inline Keys& keys() { return Keys::self(); }
inline AnyValue key(const std::string& k) { return keys().getValue(k); }
inline void key(const std::string& k, const AnyValue& v, void* o = nullptr) { keys().raiseEvent(k, v, o); }